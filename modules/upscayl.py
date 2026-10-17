# required: aiohttp
# The Python engine (used where upscayl-bin has no build, e.g. ARM Linux) also
# needs ncnn, numpy and pillow; .upsetup installs them (pip or uv) when needed.

# ⟡ why-morphy — Shizu modules

import asyncio
import contextlib
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import aiohttp
from pyrogram import Client, types

from .. import loader, utils

RELEASE_API = "https://api.github.com/repos/upscayl/upscayl-ncnn/releases/latest"
MODELS_URL = "https://raw.githubusercontent.com/upscayl/upscayl/main/resources/models/{}"

# Models shipped with the Upscayl app. All of them are 4x models; a smaller
# output scale is produced by upscayl-bin downscaling the result.
MODELS = {
    "upscayl-standard-4x": "general photos (default)",
    "upscayl-lite-4x": "fast, lighter model",
    "high-fidelity-4x": "detailed photos",
    "digital-art-4x": "illustrations, anime, art",
    "ultramix-balanced-4x": "balanced detail",
    "remacri-4x": "natural photos (non-commercial)",
    "ultrasharp-4x": "sharp detail (non-commercial)",
}

# Quality presets: (model, use TTA). Picked by comparing every model on
# downscaled photos: remacri keeps the most natural detail per second of CPU.
PRESETS = {
    "fast": ("upscayl-lite-4x", False),
    "hq": ("remacri-4x", False),
    "max": ("remacri-4x", True),
}
TTA_PASSES = 4  # Python engine; upscayl-bin's -x always does 8

# Fallback engine for systems without an upscayl-bin build (e.g. ARM Linux):
# runs the same models through the ncnn Python package in a child process
# and prints progress the way upscayl-bin does.
# Args: input output models_dir model scale format tile gpu_id tta_passes
WORKER = '''
import os
import re
import sys

import ncnn
import numpy as np
from PIL import Image, ImageOps


def output_blob(param_path):
    # The last layer's last output: "Type name n_in n_out in... out... params"
    with open(param_path) as f:
        lines = [line for line in f.read().splitlines() if line.strip()]
    fields = lines[-1].split()
    n_in, n_out = int(fields[2]), int(fields[3])
    return fields[4 + n_in + n_out - 1]


def infer(net, out_blob, rgb):
    """Runs the model on an HxWx3 uint8 array; returns HxWx3 floats in 0..1"""
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    mat = ncnn.Mat.from_pixels(rgb, ncnn.Mat.PixelType.PIXEL_RGB, w, h)
    mat.substract_mean_normalize([], [1 / 255.0] * 3)
    ex = net.create_extractor()
    ex.input("data", mat)
    ret, result = ex.extract(out_blob)
    if ret != 0:
        raise RuntimeError(f"ncnn extract failed: {ret}")
    return np.array(result).transpose(1, 2, 0)


def pick_gpu(gpu_id):
    if gpu_id == "-1":
        return None
    try:
        count = ncnn.get_gpu_count()
    except Exception:
        return None
    ids = [int(gpu_id)] if gpu_id.isdigit() else range(count)
    for i in ids:
        # Type 3 is a CPU Vulkan driver (lavapipe): plain ncnn on the CPU is faster.
        if i < count and ncnn.get_gpu_info(i).type() != 3:
            return i
    return None


# Test-time augmentation: run flipped/rotated copies and average them, which
# removes some of the model's artifacts. (flip, quarter turns) per pass;
# 4 passes keep ~3/4 of the gain of all 8 at half the cost.
TTA = {
    1: [(0, 0)],
    2: [(0, 0), (0, 2)],
    4: [(0, 0), (1, 0), (0, 2), (1, 2)],
    8: [(f, k) for f in (0, 1) for k in range(4)],
}


def auto_tile(model_bin):
    """Tile size that fits in free RAM. The big models (~33 MB) need
    roughly 4 GB at 256 px, and it grows with the square of the tile."""
    if os.path.getsize(model_bin) < 20_000_000:
        return 256
    try:
        with open("/proc/meminfo") as f:
            info = dict(line.split(":", 1) for line in f)
        free_gb = int(info["MemAvailable"].split()[0]) / 1048576
    except (OSError, KeyError, ValueError):
        return 256
    for tile, need_gb in ((256, 6), (192, 4), (128, 2.5)):
        if free_gb >= need_gb:
            return tile
    return 96


def main(src, dst, models, name, scale, fmt, tile, gpu_id, tta="1"):
    scale = int(scale)
    tile = int(tile) or auto_tile(os.path.join(models, name + ".bin"))
    passes = TTA.get(int(tta), TTA[1])
    param = os.path.join(models, name + ".param")
    match = re.search(r"([0-9])x", name)
    model_scale = int(match[1]) if match else 4

    net = ncnn.Net()
    gpu = pick_gpu(gpu_id)
    net.opt.use_vulkan_compute = gpu is not None
    if gpu is not None:
        net.set_vulkan_device(gpu)
    net.opt.num_threads = os.cpu_count() or 1
    if gpu is None:
        # ARM CPUs default to fp16 math, which visibly distorts these models.
        net.opt.use_fp16_packed = False
        net.opt.use_fp16_storage = False
        net.opt.use_fp16_arithmetic = False
        net.opt.use_bf16_storage = False
    net.load_param(param)
    net.load_model(os.path.join(models, name + ".bin"))
    out_blob = output_blob(param)

    img = ImageOps.exif_transpose(Image.open(src))
    has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
    alpha = img.convert("RGBA").getchannel("A") if has_alpha else None
    rgb = np.ascontiguousarray(np.asarray(img.convert("RGB")))
    h, w = rgb.shape[:2]
    s, pad = model_scale, 32
    out = np.zeros((h * s, w * s, 3), np.uint8)

    tiles = [(y, x) for y in range(0, h, tile) for x in range(0, w, tile)]
    for i, (y, x) in enumerate(tiles):
        # Each tile gets `pad` px of context on every side so seams don't show.
        y0, x0 = max(y - pad, 0), max(x - pad, 0)
        y1, x1 = min(y + tile + pad, h), min(x + tile + pad, w)
        patch = rgb[y0:y1, x0:x1]
        acc = 0
        for flip, turns in passes:
            x_in = np.rot90(patch[:, ::-1] if flip else patch, turns)
            y_out = np.rot90(infer(net, out_blob, x_in), -turns)
            acc = acc + (y_out[:, ::-1] if flip else y_out)
        result = (np.clip(acc / len(passes), 0, 1) * 255 + 0.5).astype(np.uint8)
        th, tw = (min(y + tile, h) - y) * s, (min(x + tile, w) - x) * s
        oy, ox = (y - y0) * s, (x - x0) * s
        out[y * s : y * s + th, x * s : x * s + tw] = result[oy : oy + th, ox : ox + tw]
        print(f"{(i + 1) / len(tiles) * 100:.2f}%", file=sys.stderr, flush=True)

    result = Image.fromarray(out)
    size = (w * scale, h * scale)
    if size != result.size:
        result = result.resize(size, Image.LANCZOS)
    if alpha is not None and fmt != "jpg":
        result.putalpha(alpha.resize(size, Image.BICUBIC))
    if fmt == "jpg":
        result.save(dst, "JPEG", quality=95)
    elif fmt == "webp":
        result.save(dst, "WEBP", quality=95)
    else:
        result.save(dst, "PNG")


try:
    main(*sys.argv[1:])
except Exception as e:
    # One clean line for the user instead of a traceback.
    print(f"error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    sys.exit(1)
'''

PY_PACKAGES = ("ncnn", "numpy", "pillow")
# Run with the userbot's own interpreter, so a fresh --user install is visible.
PY_CHECK = "import ncnn, numpy, PIL; print(ncnn.__version__)"

MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
FORMATS = ("png", "jpg", "webp")


@loader.module("Upscayl", "morphy")
class UpscaylMod(loader.Module):
    """AI image upscaling with Upscayl (upscayl-bin + Real-ESRGAN models)"""

    strings = {
        "no_reply": "🚫 Reply to a photo, an image file or a static sticker",
        "bad_args": "🚫 Usage: <code>.upscale [fast|hq|max|model] [tta] [2|3|4]</code>",
        "bad_model": "🚫 Unknown model <code>{}</code>. See <code>.upmodels</code>",
        "queued": "🕒 Waiting for the previous job to finish…",
        "installing": "📥 Downloading upscayl-bin <code>{}</code>…",
        "dl_model": "📥 Downloading model <code>{}</code>…",
        "dl_image": "📥 Downloading image…",
        "working": "⏳ Upscaling with <code>{}</code> ×{}… <b>{}</b>{}",
        "eta": " · ~{} left",
        "done": "✨ <code>{}</code> ×{} in {}",
        "cancelled": "⏹ Upscaling cancelled",
        "nothing_running": "🚫 Nothing is being upscaled right now",
        "cancelling": "⏹ Cancelling…",
        "no_binary": (
            "🚫 upscayl-bin not found and auto-install is off.\n"
            "Set <code>binary_path</code> in config or run <code>.upsetup</code>"
        ),
        "unsupported_os": "🚫 No upscayl-bin build for <code>{}</code>",
        "install_failed": "🚫 Couldn't install upscayl-bin: <code>{}</code>",
        "failed": "🚫 Upscaling failed:\n<code>{}</code>",
        "timeout": "🚫 Upscaling took longer than {}s and was stopped",
        "hint_vulkan": (
            "\n\n💡 upscayl-bin needs Vulkan. On a server without a GPU install "
            "a CPU Vulkan driver: <code>apt install libvulkan1 mesa-vulkan-drivers</code>"
        ),
        "hint_libs": (
            "\n\n💡 Missing system libraries. On Debian/Ubuntu: "
            "<code>apt install libvulkan1 libgomp1</code>"
        ),
        "hint_oom": (
            "\n\n💡 The process was killed, most likely for running out of memory. "
            "Set <code>tile_size</code> to 128 or 96 in <code>.config</code>"
        ),
        "setup_ok": "✅ Upscayl ready\n<b>Engine:</b> {}\n<b>Models:</b> <code>{}</code>",
        "engine_python": "ncnn {} via Python (no upscayl-bin build for <code>{}</code>)",
        "engine_python_forced": "ncnn {} via Python",
        "fallback": "⚠️ upscayl-bin can't run here, switching to the Python engine…",
        "py_missing": (
            "🚫 The Python engine needs <code>ncnn numpy pillow</code>, which aren't installed.\n"
            "Run <code>.upsetup</code> to install them"
        ),
        "pip_installing": "📦 Installing <code>{}</code> with {}, this can take a few minutes…",
        "pip_failed": (
            "🚫 Couldn't install <code>{packages}</code>\n"
            "<b>System:</b> <code>{system}</code>\n\n<code>{log}</code>"
        ),
        "models": (
            "🧠 <b>Quality presets</b>\n"
            "⚡ <code>fast</code> — lite model, quickest\n"
            "✨ <code>hq</code> — remacri, much more real detail, ~25–40× slower\n"
            "💎 <code>max</code> — remacri + TTA, cleanest result, ~4× slower than hq\n"
            "Add <code>tta</code> to any model for fewer artifacts (~4× slower)\n\n"
            "<b>Models</b> (default: <code>{}</code>)\n\n{}"
        ),
        "cfg_binary": "Path to upscayl-bin. Empty = auto-detect or auto-download",
        "cfg_models": "Folder with .param/.bin models. Empty = module data folder",
        "cfg_model": "Default preset (fast, hq, max) or model. Empty = upscayl-standard-4x with upscayl-bin, fast with the Python engine",
        "cfg_backend": "Engine: auto, bin (upscayl-bin) or python (ncnn package, works on ARM Linux)",
        "cfg_scale": "Default output scale: 2, 3 or 4",
        "cfg_format": "Output format: png, jpg or webp",
        "cfg_gpu": "GPU id (e.g. 0 or 0,1). Empty = auto",
        "cfg_tile": "Tile size (0 = auto, sized to free RAM). Lower it if the upscale gets killed",
        "cfg_tta": "Always use TTA: fewer artifacts, 4x slower (8x with upscayl-bin)",
        "cfg_timeout": "Max seconds for one upscale",
        "cfg_auto": "Download upscayl-bin and models automatically",
    }

    strings_ru = {
        "no_reply": "🚫 Ответь на фото, файл-картинку или статичный стикер",
        "bad_args": "🚫 Использование: <code>.upscale [fast|hq|max|модель] [tta] [2|3|4]</code>",
        "bad_model": "🚫 Неизвестная модель <code>{}</code>. Смотри <code>.upmodels</code>",
        "queued": "🕒 Жду окончания предыдущей задачи…",
        "installing": "📥 Скачиваю upscayl-bin <code>{}</code>…",
        "dl_model": "📥 Скачиваю модель <code>{}</code>…",
        "dl_image": "📥 Скачиваю картинку…",
        "working": "⏳ Апскейл через <code>{}</code> ×{}… <b>{}</b>{}",
        "eta": " · осталось ~{}",
        "done": "✨ <code>{}</code> ×{} за {}",
        "cancelled": "⏹ Апскейл отменён",
        "nothing_running": "🚫 Сейчас ничего не апскейлится",
        "cancelling": "⏹ Отменяю…",
        "no_binary": (
            "🚫 upscayl-bin не найден, автоустановка выключена.\n"
            "Укажи <code>binary_path</code> в конфиге или запусти <code>.upsetup</code>"
        ),
        "unsupported_os": "🚫 Нет сборки upscayl-bin для <code>{}</code>",
        "install_failed": "🚫 Не удалось установить upscayl-bin: <code>{}</code>",
        "failed": "🚫 Ошибка апскейла:\n<code>{}</code>",
        "timeout": "🚫 Апскейл шёл дольше {}с и был остановлен",
        "hint_vulkan": (
            "\n\n💡 upscayl-bin нужен Vulkan. На сервере без GPU поставь "
            "CPU-драйвер: <code>apt install libvulkan1 mesa-vulkan-drivers</code>"
        ),
        "hint_libs": (
            "\n\n💡 Не хватает системных библиотек. Debian/Ubuntu: "
            "<code>apt install libvulkan1 libgomp1</code>"
        ),
        "hint_oom": (
            "\n\n💡 Процесс убит системой, скорее всего не хватило памяти. "
            "Поставь <code>tile_size</code> 128 или 96 в <code>.config</code>"
        ),
        "setup_ok": "✅ Upscayl готов\n<b>Движок:</b> {}\n<b>Модели:</b> <code>{}</code>",
        "engine_python": "ncnn {} через Python (нет сборки upscayl-bin для <code>{}</code>)",
        "engine_python_forced": "ncnn {} через Python",
        "fallback": "⚠️ upscayl-bin здесь не запускается, переключаюсь на Python-движок…",
        "py_missing": (
            "🚫 Python-движку нужны <code>ncnn numpy pillow</code>, они не установлены.\n"
            "Запусти <code>.upsetup</code>, чтобы их установить"
        ),
        "pip_installing": "📦 Устанавливаю <code>{}</code> через {}, это может занять пару минут…",
        "pip_failed": (
            "🚫 Не удалось установить <code>{packages}</code>\n"
            "<b>Система:</b> <code>{system}</code>\n\n<code>{log}</code>"
        ),
        "models": (
            "🧠 <b>Пресеты качества</b>\n"
            "⚡ <code>fast</code> — модель lite, быстрее всего\n"
            "✨ <code>hq</code> — remacri, намного больше реальных деталей, ~в 25–40 раз медленнее\n"
            "💎 <code>max</code> — remacri + TTA, самый чистый результат, ~в 4 раза медленнее hq\n"
            "Добавь <code>tta</code> к любой модели — меньше артефактов (~в 4 раза медленнее)\n\n"
            "<b>Модели</b> (по умолчанию: <code>{}</code>)\n\n{}"
        ),
        "cfg_binary": "Путь к upscayl-bin. Пусто = найти или скачать автоматически",
        "cfg_models": "Папка с моделями .param/.bin. Пусто = папка модуля",
        "cfg_model": "Пресет (fast, hq, max) или модель по умолчанию. Пусто = upscayl-standard-4x для upscayl-bin, fast для Python-движка",
        "cfg_backend": "Движок: auto, bin (upscayl-bin) или python (пакет ncnn, работает на ARM Linux)",
        "cfg_scale": "Масштаб по умолчанию: 2, 3 или 4",
        "cfg_format": "Формат результата: png, jpg или webp",
        "cfg_gpu": "ID видеокарты (например 0 или 0,1). Пусто = авто",
        "cfg_tile": "Размер тайла (0 = авто, по свободной памяти). Уменьши, если апскейл убивается",
        "cfg_tta": "Всегда использовать TTA: меньше артефактов, в 4 раза медленнее (в 8 с upscayl-bin)",
        "cfg_timeout": "Максимум секунд на один апскейл",
        "cfg_auto": "Скачивать upscayl-bin и модели автоматически",
    }

    @staticmethod
    def _rich_status(title: str, text: str) -> dict:
        """A visual status card with the original text retained as fallback."""
        return {
            "html": (
                f"<h2>{title}</h2>"
                f"<blockquote>{text.replace(chr(10), '<br>')}</blockquote>"
            )
        }

    def __init__(self):
        self.config = loader.ModuleConfig(
            "binary_path", "", lambda: self.strings("cfg_binary"),
            "models_path", "", lambda: self.strings("cfg_models"),
            "backend", "auto", lambda: self.strings("cfg_backend"),
            "model", "", lambda: self.strings("cfg_model"),
            "scale", 4, lambda: self.strings("cfg_scale"),
            "format", "png", lambda: self.strings("cfg_format"),
            "gpu_id", "", lambda: self.strings("cfg_gpu"),
            "tile_size", 0, lambda: self.strings("cfg_tile"),
            "tta", False, lambda: self.strings("cfg_tta"),
            "timeout", 3600, lambda: self.strings("cfg_timeout"),
            "auto_install", True, lambda: self.strings("cfg_auto"),
        )  # fmt: skip
        # Set here, not in on_load: Shizu inspects every attribute while
        # registering the module, before on_load runs.
        self.data_dir = Path.home() / ".shizu-upscayl"
        self._proc = None
        self._bin_unusable = False
        self._cancelled = False

    async def on_load(self, app: Client):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._install_lock = asyncio.Lock()

    async def on_unload(self):
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()

    # ---------------------------------------------------------------- paths

    def _models_dir(self) -> Path:
        path = Path(self.config["models_path"] or self.data_dir / "models")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _os_name() -> str:
        if sys.platform == "darwin":
            return "macos"
        if sys.platform.startswith("win"):
            return "windows"
        if sys.platform.startswith("linux"):
            return "linux"
        return sys.platform

    def _exe_name(self) -> str:
        return "upscayl-bin.exe" if self._os_name() == "windows" else "upscayl-bin"

    def _find_binary(self):
        if custom := self.config["binary_path"]:
            return Path(custom).expanduser() if Path(custom).expanduser().is_file() else None

        candidates = list((self.data_dir / "bin").rglob(self._exe_name()))
        if found := shutil.which("upscayl-bin"):
            candidates.append(Path(found))
        if self._os_name() == "macos":
            candidates.append(
                Path("/Applications/Upscayl.app/Contents/Resources/bin/upscayl-bin")
            )
        return next((c for c in candidates if c.is_file()), None)

    # ------------------------------------------------------------- installing

    async def _download(self, session: aiohttp.ClientSession, url: str, dest: Path):
        tmp = dest.with_name(dest.name + ".part")
        async with session.get(url) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 16):
                    f.write(chunk)
        tmp.replace(dest)

    def _platform(self) -> str:
        return f"{self._os_name()}/{platform.machine().lower()}"

    def _bin_supported(self) -> bool:
        """Whether upscayl-ncnn publishes a build for this system"""
        os_name = self._os_name()
        if os_name == "linux":
            return platform.machine().lower() in ("x86_64", "amd64")
        return os_name in ("macos", "windows")

    async def _install_binary(self, message) -> Path:
        os_name = self._os_name()
        if not self._bin_supported():
            raise RuntimeError(self.strings("unsupported_os").format(self._platform()))

        async with aiohttp.ClientSession() as session:
            async with session.get(RELEASE_API) as resp:
                resp.raise_for_status()
                release = await resp.json()

            asset = next(
                (a for a in release["assets"] if a["name"].endswith(f"-{os_name}.zip")),
                None,
            )
            if not asset:
                raise RuntimeError(self.strings("unsupported_os").format(os_name))

            await utils.answer(
                message, self.strings("installing").format(release["tag_name"])
            )
            bin_dir = self.data_dir / "bin"
            shutil.rmtree(bin_dir, ignore_errors=True)
            bin_dir.mkdir(parents=True)
            archive = self.data_dir / asset["name"]
            await self._download(session, asset["browser_download_url"], archive)

        await utils.run_sync(self._extract, archive, bin_dir)
        archive.unlink(missing_ok=True)

        exe = next(bin_dir.rglob(self._exe_name()), None)
        if not exe:
            raise RuntimeError(f"{self._exe_name()} not found in {asset['name']}")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.set("version", release["tag_name"])
        return exe

    @staticmethod
    def _extract(archive: Path, dest: Path):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)

    async def _ensure_binary(self, message) -> Path:
        async with self._install_lock:
            if exe := self._find_binary():
                return exe
            if self.config["binary_path"] or not self.config["auto_install"]:
                raise RuntimeError(self.strings("no_binary"))
            try:
                return await self._install_binary(message)
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError(
                    self.strings("install_failed").format(utils.escape_html(str(e)))
                ) from e

    async def _choose_backend(self, message) -> tuple:
        """("bin", path to upscayl-bin) or ("python", None)"""
        choice = str(self.config["backend"]).lower()
        if choice == "python":
            return "python", None
        if choice == "bin" or self.config["binary_path"]:
            return "bin", await self._ensure_binary(message)
        if not self._bin_unusable and (
            self._find_binary() or (self.config["auto_install"] and self._bin_supported())
        ):
            return "bin", await self._ensure_binary(message)
        return "python", None

    def _auto_fallback(self) -> bool:
        """Whether a failing upscayl-bin may be replaced by the Python engine"""
        return str(self.config["backend"]).lower() == "auto" and not self.config["binary_path"]

    def _default_choice(self, backend: str) -> tuple:
        """(model, tta) to use when the command names neither"""
        if choice := self._resolve_choice(str(self.config["model"]).strip()):
            return choice
        # Heavy models are ~20x slower than lite: too slow by default on a CPU.
        if backend == "python":
            return PRESETS["fast"]
        return "upscayl-standard-4x", False

    def _resolve_choice(self, word: str):
        """A preset name or model name -> (model, tta), else None"""
        if not word:
            return None
        if word.lower() in PRESETS:
            return PRESETS[word.lower()]
        if model := self._resolve_model(word):
            return model, False
        return None

    def _engine_text(self, backend: str, exe, ncnn_version: str = "") -> str:
        if backend == "bin":
            return f"<code>{utils.escape_html(str(exe))}</code>"
        if self._bin_supported():
            return self.strings("engine_python_forced").format(ncnn_version)
        return self.strings("engine_python").format(ncnn_version, self._platform())

    async def _exec(self, args: list, timeout: int) -> tuple:
        """(return code, combined output) of a short helper process"""
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return -1, f"timed out after {timeout}s"
        return proc.returncode, out.decode(errors="replace")

    async def _python_engine_version(self):
        """ncnn version if the Python engine can run, else None"""
        code, out = await self._exec([sys.executable, "-c", PY_CHECK], 60)
        lines = out.strip().splitlines()
        return lines[-1] if code == 0 and lines else None

    @staticmethod
    def _find_uv():
        # A userbot started as a service often lacks ~/.local/bin in PATH.
        for path in (
            shutil.which("uv"),
            Path.home() / ".local" / "bin" / "uv",
            Path.home() / ".cargo" / "bin" / "uv",
        ):
            if path and Path(path).is_file():
                return str(path)
        return None

    async def _installer(self) -> list:
        """Command that installs packages into the userbot's interpreter"""
        # Same flags Shizu uses for module requirements.
        pip = [sys.executable, "-m", "pip", "install"]
        if sys.prefix == sys.base_prefix:
            pip.append("--user")
        if (await self._exec([sys.executable, "-m", "pip", "--version"], 60))[0] == 0:
            return pip
        # Virtualenvs made by uv have no pip.
        if uv := self._find_uv():
            return [uv, "pip", "install", "--python", sys.executable]
        await self._exec([sys.executable, "-m", "ensurepip"], 300)
        return pip  # if ensurepip failed too, pip's own error explains why

    async def _install_python_engine(self, message) -> str:
        """Installs the Python engine's packages; returns the ncnn version"""
        installer = await self._installer()
        tool = "uv" if installer[1:2] == ["pip"] else "pip"
        await utils.answer(
            message,
            self.strings("pip_installing").format(" ".join(PY_PACKAGES), tool),
        )
        code, out = await self._exec([*installer, *PY_PACKAGES], 1800)
        if version := await self._python_engine_version():
            return version
        system = (
            f"{self._platform()}, Python {platform.python_version()}, "
            f"{' '.join(filter(None, platform.libc_ver())) or 'unknown libc'}"
        )
        raise RuntimeError(
            self.strings("pip_failed").format(
                packages=" ".join(PY_PACKAGES),
                system=utils.escape_html(system),
                log=utils.escape_html(out.strip()[-1500:] or f"exit code {code}"),
            )
        )

    async def _ensure_model(self, message, model: str):
        files = [self._models_dir() / f"{model}.{ext}" for ext in ("param", "bin")]
        if all(f.is_file() for f in files):
            return
        if model not in MODELS or not self.config["auto_install"]:
            raise RuntimeError(self.strings("bad_model").format(utils.escape_html(model)))

        await utils.answer(message, self.strings("dl_model").format(model))
        async with self._install_lock, aiohttp.ClientSession() as session:
            for f in files:
                if not f.is_file():
                    await self._download(session, MODELS_URL.format(f.name), f)

    # ---------------------------------------------------------------- models

    def _local_models(self) -> list:
        return sorted(
            p.stem
            for p in self._models_dir().glob("*.param")
            if p.with_suffix(".bin").is_file()
        )

    def _resolve_model(self, name: str):
        """Accepts a full model name or a short one like `standard`/`digital-art`"""
        name = name.lower()
        names = list(dict.fromkeys([*MODELS, *self._local_models()]))
        if name in names:
            return name

        def short(n):
            return re.sub(r"^upscayl-|-\dx$", "", n)

        matches = [n for n in names if short(n) == name] or [
            n for n in names if short(n).startswith(name)
        ]
        return matches[0] if len(matches) == 1 else None

    # --------------------------------------------------------------- running

    async def _run(self, args: list, on_progress) -> str:
        """Runs upscayl-bin, reports progress, returns its stderr output"""
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        log = []

        async def read():
            buf = b""
            while chunk := await self._proc.stderr.read(1024):
                buf += chunk
                *lines, buf = re.split(rb"[\r\n]", buf)
                for line in lines:
                    text = line.decode(errors="replace").strip()
                    if not text:
                        continue
                    if re.fullmatch(r"\d+(\.\d+)?%", text):
                        await on_progress(text)
                    else:
                        log.append(text)
            await self._proc.wait()

        try:
            await asyncio.wait_for(read(), timeout=int(self.config["timeout"]))
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._proc.wait()
            raise
        return "\n".join(log)

    def _error_text(self, log: str, backend: str, killed: bool = False) -> str:
        if killed:
            # SIGKILL with no output of its own: the kernel's OOM killer.
            return self.strings("failed").format(
                utils.escape_html(log[-500:] or "killed")
            ) + self.strings("hint_oom")
        if backend == "python":
            # Skip ncnn's Vulkan probing noise; the worker ends with "error: ...".
            errors = [line for line in log.splitlines() if line.startswith("error: ")]
            log = errors[-1][len("error: ") :] if errors else log
        text = self.strings("failed").format(utils.escape_html(log[-1500:] or "no output"))
        if backend != "bin":
            return text
        if "shared librar" in log.lower():
            text += self.strings("hint_libs")
        elif self._is_env_error(log):
            text += self.strings("hint_vulkan")
        return text

    @staticmethod
    def _is_env_error(log: str) -> bool:
        """upscayl-bin failed because of the system (Vulkan, libraries), not the image"""
        lowered = log.lower()
        return any(
            s in lowered for s in ("vk", "vulkan", "gpu", "shared librar", "exec format")
        )

    def _worker_path(self) -> Path:
        path = self.data_dir / "ncnn_worker.py"
        if not path.is_file() or path.read_text() != WORKER:
            path.write_text(WORKER)
        return path

    def _build_args(self, backend, exe, src, dst, model, scale, fmt, tta) -> list:
        tile = int(self.config["tile_size"] or 0)
        gpu = str(self.config["gpu_id"]).strip()
        if backend == "python":
            return [
                sys.executable, str(self._worker_path()),
                str(src), str(dst), str(self._models_dir()), model,
                str(scale), fmt, str(tile), gpu,
                str(TTA_PASSES if tta else 1),
            ]  # fmt: skip
        args = [
            str(exe),
            "-i", str(src),
            "-o", str(dst),
            "-m", str(self._models_dir()),
            "-n", model,
            "-s", str(scale),
            "-f", fmt,
        ]  # fmt: skip
        if gpu:
            args += ["-g", gpu]
        if tile:
            args += ["-t", str(tile)]
        if tta:
            args.append("-x")
        return args

    @staticmethod
    def _input_ext(msg: types.Message):
        if msg.photo:
            return "jpg"
        if msg.sticker and not (msg.sticker.is_animated or msg.sticker.is_video):
            return "webp"
        if msg.document:
            return MIME_EXT.get(msg.document.mime_type or "")
        return None

    # -------------------------------------------------------------- commands

    @loader.command()
    async def upscale(self, app: Client, message: types.Message):
        """Upscale the replied image: .upscale [fast|hq|max|model] [tta] [2|3|4]"""
        reply = message.reply_to_message
        if not reply or not (ext := self._input_ext(reply)):
            return await utils.answer(message, self.strings("no_reply"))

        choice, tta, scale = None, False, int(self.config["scale"])
        for arg in utils.get_args_raw(message).split():
            if arg in ("2", "3", "4"):
                scale = int(arg)
            elif arg.lower() == "tta":
                tta = True
            elif resolved := self._resolve_choice(arg):
                choice = resolved
            else:
                return await utils.answer(
                    message, self.strings("bad_model").format(utils.escape_html(arg))
                )

        fmt = str(self.config["format"]).lower()
        if fmt not in FORMATS:
            fmt = "png"

        if self._lock.locked():
            await utils.answer(message, self.strings("queued"))

        async with self._lock:
            try:
                backend, exe = await self._choose_backend(message)
            except RuntimeError as e:
                return await utils.answer(message, str(e))

            with tempfile.TemporaryDirectory(prefix="upscayl-") as tmp:
                src = Path(tmp) / f"input.{ext}"
                await utils.answer(message, self.strings("dl_image"))
                await app.download_media(reply, file_name=str(src))

                started = time.monotonic()
                result = await self._upscale_with(
                    message, backend, exe, src, Path(tmp), choice, tta, scale, fmt
                )
                if result is None and backend == "bin" and self._bin_unusable:
                    # upscayl-bin exists but can't run here: retry with ncnn.
                    await utils.answer(message, self.strings("fallback"))
                    started = time.monotonic()
                    result = await self._upscale_with(
                        message, "python", None, src, Path(tmp), choice, tta, scale, fmt
                    )
                if result is None:
                    return

                dst, label = result
                # Sent as a document so Telegram doesn't recompress the result.
                await utils.answer_file(
                    message,
                    str(dst),
                    caption=self.strings("done").format(
                        label, scale, self._duration(time.monotonic() - started)
                    ),
                )

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = int(seconds + 0.5)
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m {seconds % 60:02d}s"
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"

    async def _upscale_with(self, message, backend, exe, src, tmp, choice, tta, scale, fmt):
        """Runs one engine. Returns (output path, label), or None once the error is
        reported (or, for an upscayl-bin that can't run here, left for the fallback)"""
        model, preset_tta = choice or self._default_choice(backend)
        tta = tta or preset_tta or bool(self.config["tta"])
        label = f"{model} +TTA" if tta else model
        if backend == "python" and not await self._python_engine_version():
            await utils.answer(message, self.strings("py_missing"))
            return None
        try:
            await self._ensure_model(message, model)
        except RuntimeError as e:
            await utils.answer(message, str(e))
            return None

        dst = tmp / f"upscayl_{model}_x{scale}.{fmt}"
        dst.unlink(missing_ok=True)
        args = self._build_args(backend, exe, src, dst, model, scale, fmt, tta)
        started = last_edit = time.monotonic()

        async def on_progress(percent: str):
            nonlocal last_edit
            now = time.monotonic()
            # Long jobs edit less often to stay clear of Telegram's flood limits.
            if now - last_edit < (3 if now - started < 60 else 10):
                return
            last_edit = now
            done = float(percent.rstrip("%"))
            eta = ""
            if 0 < done < 100:
                left = (now - started) * (100 - done) / done
                eta = self.strings("eta").format(self._duration(left))
            with contextlib.suppress(Exception):
                await utils.answer(
                    message, self.strings("working").format(label, scale, percent, eta)
                )

        await utils.answer(
            message, self.strings("working").format(label, scale, "0%", "")
        )
        self._cancelled = False
        killed = False
        try:
            log = await self._run(args, on_progress)
            killed = self._proc.returncode == -9 and not self._cancelled
        except asyncio.TimeoutError:
            await utils.answer(
                message, self.strings("timeout").format(self.config["timeout"])
            )
            return None
        except OSError as e:
            log = str(e)
        finally:
            self._proc = None

        if self._cancelled:
            await utils.answer(message, self.strings("cancelled"))
            return None
        if dst.is_file() and dst.stat().st_size:
            return dst, label

        if (
            backend == "bin"
            and not killed
            and self._auto_fallback()
            and self._is_env_error(log)
        ):
            self._bin_unusable = True
            return None
        await utils.answer(message, self._error_text(log, backend, killed))
        return None

    @loader.command()
    async def upmodels(self, app: Client, message: types.Message):
        """List available upscaling models"""
        local = set(self._local_models())
        lines = [
            f"{'✅' if name in local else '☁️'} <code>{name}</code> — {desc}"
            for name, desc in MODELS.items()
        ]
        lines += [f"✅ <code>{n}</code> — custom" for n in sorted(local - set(MODELS))]
        text = self.strings("models").format(
            utils.escape_html(str(self.config["model"]) or "auto"), "\n".join(lines)
        )
        await utils.answer(
            message, text, rich_message=self._rich_status("🧠 Upscayl models", text)
        )

    @loader.command()
    async def upcancel(self, app: Client, message: types.Message):
        """Stop the upscale that is running now"""
        if not self._proc or self._proc.returncode is not None:
            return await utils.answer(message, self.strings("nothing_running"))
        self._cancelled = True
        with contextlib.suppress(ProcessLookupError):
            self._proc.kill()
        await utils.answer(message, self.strings("cancelling"))

    @loader.command()
    async def upsetup(self, app: Client, message: types.Message):
        """Install the engine and the default model ahead of time"""
        version = ""
        try:
            backend, exe = await self._choose_backend(message)
            if backend == "python" and not (
                version := await self._python_engine_version()
            ):
                version = await self._install_python_engine(message)
            await self._ensure_model(message, self._default_choice(backend)[0])
        except RuntimeError as e:
            return await utils.answer(message, str(e))
        text = self.strings("setup_ok").format(
            self._engine_text(backend, exe, version),
            utils.escape_html(str(self._models_dir())),
        )
        await utils.answer(
            message, text, rich_message=self._rich_status("✅ Upscayl ready", text)
        )
