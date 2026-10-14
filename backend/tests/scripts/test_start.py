"""全栈启动的数据包解压、重复执行、嵌入模型核对与 Compose 挂载路径。"""

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts import start

FILES = {
    "flowpay-dataset-v1/data/knowledge/example.md": "knowledge",
    "flowpay-dataset-v1/benchmark/design/example.json": "{}",
}
# ollama list 的真实排版：首行是表头，第二列是 12 位短 digest。
DIGEST = "ac6da0dfba84"
MODEL_LIST = (
    "NAME                    ID              SIZE      MODIFIED\n"
    f"qwen3-embedding:0.6b    {DIGEST}    639 MB    2 minutes ago\n"
)


def write_config(root: Path, revision: str) -> None:
    """写出 start.py 读取嵌入模型标签与 digest 所需的最小 local.toml。"""
    config = root / start.CONFIG
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f'[embedding]\nname = "qwen3-embedding:0.6b"\nrevision = "{revision}"\n')


def write_bundle(path: Path, files: dict[str, str] | None = None) -> Path:
    """创建包含发布目录结构的临时包，不依赖真实数据集。"""
    entries = FILES if files is None else files
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
    else:
        with tarfile.open(path, "w:gz") as archive:
            for name, content in entries.items():
                data = content.encode()
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
    return path


@pytest.mark.parametrize("suffix", [".zip", ".ZIP", ".tar.gz"])
def test_resolve_dataset_unpacks_bundle(tmp_path: Path, suffix: str) -> None:
    bundle = write_bundle(tmp_path / f"dataset{suffix}")
    target = start.resolve_dataset(str(bundle))
    assert target == tmp_path / "flowpay-dataset-v1"
    for name, content in FILES.items():
        assert (tmp_path / name).read_text() == content


@pytest.mark.parametrize("suffix", [".zip", ".tar.gz"])
def test_repeat_start_reuses_extracted_dataset(tmp_path: Path, suffix: str) -> None:
    bundle = write_bundle(tmp_path / f"dataset{suffix}")
    target = start.resolve_dataset(str(bundle))
    document = target / "data/knowledge/example.md"
    document.write_text("local changes")
    assert start.resolve_dataset(str(bundle)) == target
    assert document.read_text() == "local changes"
    assert start.resolve_dataset(str(target)) == target


def test_relative_zip_path_is_resolved_from_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start, "ROOT", tmp_path)
    directory = tmp_path / "data packages"
    directory.mkdir()
    write_bundle(directory / "dataset.zip")
    assert start.resolve_dataset("data packages/dataset.zip") == directory / "flowpay-dataset-v1"


def test_zip_requires_dataset_layout(tmp_path: Path) -> None:
    bundle = write_bundle(
        tmp_path / "dataset.zip", {"flowpay-dataset-v1/data/knowledge/example.md": "knowledge"}
    )
    with pytest.raises(SystemExit, match="缺少：benchmark/design"):
        start.resolve_dataset(str(bundle))


def test_zip_ignores_macos_metadata(tmp_path: Path) -> None:
    bundle = write_bundle(
        tmp_path / "dataset.zip",
        {
            **FILES,
            "__MACOSX/._flowpay-dataset-v1": "resource fork",
            "__MACOSX/flowpay-dataset-v1/._data": "resource fork",
            ".DS_Store": "directory cache",
            "flowpay-dataset-v1/.DS_Store": "directory cache",
            "flowpay-dataset-v1/data/.DS_Store": "directory cache",
        },
    )
    target = start.resolve_dataset(str(bundle))
    assert target == tmp_path / "flowpay-dataset-v1"
    for name, content in FILES.items():
        assert (tmp_path / name).read_text() == content
    assert not (tmp_path / "__MACOSX").exists()
    assert not (tmp_path / ".DS_Store").exists()
    assert list(target.rglob(".DS_Store")) == []


@pytest.mark.parametrize("encoding,filename", [("utf-8", "文档.md"), ("cp437", "café.md")])
def test_zip_preserves_filenames_without_encoding_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str, filename: str
) -> None:
    def encode_name(info: zipfile.ZipInfo) -> tuple[bytes, int]:
        return info.filename.encode(encoding), info.flag_bits & ~0x800

    # 模拟旧打包器：写入非 ASCII 文件名，却没有设置 ZIP 的 UTF-8 标志。
    with monkeypatch.context() as patch:
        patch.setattr(zipfile.ZipInfo, "_encodeFilenameFlags", encode_name)
        bundle = write_bundle(
            tmp_path / "dataset.zip", {**FILES, f"flowpay-dataset-v1/{filename}": "document"}
        )
    target = start.resolve_dataset(str(bundle))
    assert (target / filename).read_text() == "document"


def test_zip_rejects_multiple_top_level_directories(tmp_path: Path) -> None:
    bundle = write_bundle(tmp_path / "dataset.zip", {**FILES, "other/file.txt": "other"})
    with pytest.raises(SystemExit, match="只有一个顶层目录"):
        start.resolve_dataset(str(bundle))
    assert not (tmp_path / "flowpay-dataset-v1").exists()
    assert not (tmp_path / "other").exists()


@pytest.mark.parametrize(
    "name", ["flowpay-dataset-v1/../escaped.txt", "flowpay-dataset-v1/..\\escaped.txt"]
)
def test_zip_rejects_unsafe_paths_before_writing(tmp_path: Path, name: str) -> None:
    bundle = write_bundle(tmp_path / "dataset.zip", {**FILES, name: "unexpected"})
    with pytest.raises(SystemExit, match="不安全的路径"):
        start.resolve_dataset(str(bundle))
    assert not (tmp_path / "flowpay-dataset-v1").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_zip_rejects_existing_symlink_escape(tmp_path: Path) -> None:
    target = tmp_path / "flowpay-dataset-v1"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "data").symlink_to(outside, target_is_directory=True)
    bundle = write_bundle(tmp_path / "dataset.zip")
    with pytest.raises(SystemExit, match="不安全的路径"):
        start.resolve_dataset(str(bundle))
    assert list(outside.iterdir()) == []


def prepare_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str = DIGEST + "a" * 52
) -> tuple[list[tuple[str, ...]], list[str], Path]:
    """装好一次 start.main 所需的 .env、数据包与配置，并替换掉所有 docker 调用。"""
    monkeypatch.setattr(start, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["start.py"])
    monkeypatch.delenv(start.DATASET_KEY, raising=False)
    monkeypatch.delenv(start.MODEL_KEY, raising=False)
    write_bundle(tmp_path / "dataset.zip")
    write_config(tmp_path, revision)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FLOWPAY_DATASET_ROOT='dataset.zip'\n"
        "FLOWPAY_MODEL_API_KEY='example'\n"
        "OLLAMA_API_KEY='ollama'\n"
    )
    calls: list[tuple[str, ...]] = []
    dataset_paths: list[str] = []

    def ready(env: dict[str, str]) -> bool:
        dataset_paths.append(env[start.DATASET_KEY])
        return True

    def compose(*args: str, env: dict[str, str] | None = None) -> None:
        assert env is not None
        calls.append(args)
        dataset_paths.append(env[start.DATASET_KEY])

    def capture(*args: str, env: dict[str, str] | None = None) -> str:
        return MODEL_LIST if args[-1] == "list" else ""

    monkeypatch.setattr(start, "knowledge_ready", ready)
    monkeypatch.setattr(start, "compose", compose)
    monkeypatch.setattr(start, "capture", capture)
    return calls, dataset_paths, env_file


def test_start_passes_extracted_zip_directory_to_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, dataset_paths, env_file = prepare_start(tmp_path, monkeypatch)
    original = env_file.read_bytes()
    start.main()
    # 嵌入服务在后端之前就位：模型没下载完就起后端，第一个问题仍会失败。
    assert calls == [
        ("up", "-d", "--wait", "postgres"),
        ("up", "-d", "--wait", "ollama"),
        ("run", "--rm", "--no-deps", "ollama-init"),
        ("up", "-d"),
    ]
    assert dataset_paths == [str(tmp_path / "flowpay-dataset-v1")] * 5
    assert env_file.read_bytes() == original


def test_start_stops_when_embedding_digest_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同名标签被上游换过就不能继续：数据包里的向量属于原模型。"""
    calls, _, _ = prepare_start(tmp_path, monkeypatch, revision="b" * 64)
    with pytest.raises(SystemExit, match="数据包里的向量属于原模型"):
        start.main()
    assert ("up", "-d") not in calls
