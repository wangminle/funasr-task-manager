from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SUPPORTED_EXTENSIONS = {
    ".wav": "audio",
    ".mp3": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    ".webm": "video",
    ".mp4": "video",
    ".mkv": "video",
    ".avi": "video",
    ".mov": "video",
}

LOSSLESS_AUDIO_EXTENSIONS = {".wav", ".flac"}
COMPRESSED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg"}
VIDEO_EXTENSIONS = {".webm", ".mp4", ".mkv", ".avi", ".mov"}
SMOKE_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
REMOTE_STANDARD_TARGET_COUNT = 5


@dataclass(frozen=True)
class FixtureFile:
    path: Path
    workspace_root: Path
    media_type: str
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def extension(self) -> str:
        return self.path.suffix.lower()

    @property
    def relative_path(self) -> str:
        return self.path.relative_to(self.workspace_root).as_posix()

    @property
    def size_human(self) -> str:
        size = float(self.size_bytes)
        units = ["B", "KB", "MB", "GB"]
        for unit in units:
            if size < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{self.size_bytes} B"


def resolve_workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_assets_root(workspace_root: Path) -> Path:
    return workspace_root / "7-data" / "assets" / "1-测试audioFiles"


def default_output_path(workspace_root: Path, profile: str) -> Path:
    return workspace_root / "7-data" / "outputs" / "e2e" / "fixture-batches" / f"{profile}.json"


def discover_files(assets_root: Path, workspace_root: Path) -> list[FixtureFile]:
    fixtures: list[FixtureFile] = []
    for path in sorted(assets_root.rglob("*")):
        if not path.is_file():
            continue
        extension = path.suffix.lower()
        media_type = SUPPORTED_EXTENSIONS.get(extension)
        if not media_type:
            continue
        fixtures.append(
            FixtureFile(
                path=path.resolve(),
                workspace_root=workspace_root.resolve(),
                media_type=media_type,
                size_bytes=path.stat().st_size,
            )
        )
    return fixtures


def choose_first(candidates: list[FixtureFile], chosen: list[tuple[FixtureFile, str]], reason: str) -> None:
    taken = {item.path for item, _ in chosen}
    for candidate in candidates:
        if candidate.path in taken:
            continue
        chosen.append((candidate, reason))
        return


def choose_smoke(fixtures: list[FixtureFile]) -> list[tuple[FixtureFile, str]]:
    by_smallest = sorted(fixtures, key=lambda item: (item.size_bytes, item.name.lower()))
    smoke_pool = [item for item in by_smallest if item.size_bytes <= SMOKE_MAX_FILE_SIZE_BYTES]
    candidate_pool = smoke_pool or by_smallest
    chosen: list[tuple[FixtureFile, str]] = []

    choose_first(
        [item for item in candidate_pool if item.extension in LOSSLESS_AUDIO_EXTENSIONS],
        chosen,
        "覆盖无损音频上传链路",
    )
    choose_first(
        [item for item in candidate_pool if item.extension in COMPRESSED_AUDIO_EXTENSIONS],
        chosen,
        "覆盖压缩音频上传链路",
    )
    choose_first(
        [item for item in candidate_pool if item.extension in VIDEO_EXTENSIONS],
        chosen,
        "覆盖视频转写与预处理链路",
    )

    for candidate in candidate_pool:
        if len(chosen) >= min(3, len(candidate_pool)):
            break
        choose_first([candidate], chosen, "补足 smoke 最小回归样本")
    return chosen


def choose_standard(fixtures: list[FixtureFile]) -> list[tuple[FixtureFile, str]]:
    chosen = choose_smoke(fixtures)
    target_count = min(5, len(fixtures))
    by_largest = sorted(fixtures, key=lambda item: (-item.size_bytes, item.name.lower()))
    by_smallest = sorted(fixtures, key=lambda item: (item.size_bytes, item.name.lower()))

    choose_first(
        [item for item in by_largest if item.media_type == "audio"],
        chosen,
        "补充较大音频样本以覆盖更长转写场景",
    )
    choose_first(
        [item for item in by_largest if item.media_type == "video"],
        chosen,
        "补充较大视频样本以覆盖更重预处理场景",
    )

    used_extensions = {item.extension for item, _ in chosen}
    for candidate in by_smallest:
        if len(chosen) >= target_count:
            break
        if candidate.extension in used_extensions:
            continue
        choose_first([candidate], chosen, "补充额外格式多样性")
        used_extensions = {item.extension for item, _ in chosen}

    for candidate in by_smallest:
        if len(chosen) >= target_count:
            break
        choose_first([candidate], chosen, "补足 standard 回归样本")
    return chosen


def choose_remote_standard(fixtures: list[FixtureFile]) -> list[tuple[FixtureFile, str]]:
    chosen: list[tuple[FixtureFile, str]] = []
    by_smallest = sorted(fixtures, key=lambda item: (item.size_bytes, item.name.lower()))

    for index, candidate in enumerate(by_smallest[: min(REMOTE_STANDARD_TARGET_COUNT, len(by_smallest))], start=1):
        chosen.append((candidate, f"按体积从小到大选择的第 {index} 个样本"))

    return chosen


def choose_full(fixtures: list[FixtureFile]) -> list[tuple[FixtureFile, str]]:
    return [(fixture, "纳入 full 全量回归批次") for fixture in sorted(fixtures, key=lambda item: item.name.lower())]


def build_manifest(
    fixtures: list[FixtureFile],
    profile: str,
    workspace_root: Path,
    assets_root: Path,
) -> dict:
    if profile == "smoke":
        chosen = choose_smoke(fixtures)
    elif profile == "remote-standard":
        chosen = choose_remote_standard(fixtures)
    elif profile == "standard":
        chosen = choose_standard(fixtures)
    else:
        chosen = choose_full(fixtures)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "workspace_root": str(workspace_root),
        "assets_root": str(assets_root),
        "selected_count": len(chosen),
        "selected_total_bytes": sum(item.size_bytes for item, _ in chosen),
        "files": [
            {
                "name": item.name,
                "relative_path": item.relative_path,
                "absolute_path": str(item.path),
                "extension": item.extension,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
                "size_human": item.size_human,
                "selection_reason": reason,
            }
            for item, reason in chosen
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="为 funasr-task-manager 生成可复用的浏览器 E2E 测试素材批次。"
    )
    parser.add_argument("--profile", choices=["smoke", "remote-standard", "standard", "full"], default="smoke")
    parser.add_argument("--assets-root", type=Path, help="测试素材目录，默认使用 7-data/assets/1-测试audioFiles")
    parser.add_argument("--output", type=Path, help="将结果写入指定 JSON 文件")
    parser.add_argument("--write", action="store_true", help="写入默认输出路径 7-data/outputs/e2e/fixture-batches/<profile>.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace_root = resolve_workspace_root()
    assets_root = (args.assets_root or default_assets_root(workspace_root)).resolve()

    if not assets_root.exists():
        raise SystemExit(f"素材目录不存在: {assets_root}")

    fixtures = discover_files(assets_root, workspace_root)
    if not fixtures:
        raise SystemExit(f"素材目录中没有可用的音视频文件: {assets_root}")

    manifest = build_manifest(
        fixtures=fixtures,
        profile=args.profile,
        workspace_root=workspace_root,
        assets_root=assets_root,
    )
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)

    output_path = args.output
    if output_path is None and args.write:
        output_path = default_output_path(workspace_root, args.profile)

    if output_path is not None:
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")

    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
