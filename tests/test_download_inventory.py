"""Downloads-folder inventory tests (bounded, nested, no FastAPI needed)."""

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
for _p in (_BASE, os.path.join(_BASE, "backend"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.download_inventory import scan_download_tree  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def test_nested_tree():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "Novel A"))
    os.makedirs(os.path.join(root, "Novel A", "extras"))
    with open(os.path.join(root, "Novel A", "vol1.epub"), "wb") as fh:
        fh.write(b"x" * 2048)
    with open(os.path.join(root, "Novel A", "extras", "cover.jpg"), "wb") as fh:
        fh.write(b"y" * 1024)
    with open(os.path.join(root, "loose.epub"), "wb") as fh:
        fh.write(b"z" * 512)

    tree = scan_download_tree(root)
    check(tree["error"] == "", tree)
    check(tree["file_count"] == 3, tree)
    check(tree["folder_count"] == 2, tree)
    check(tree["total_size"] == 3584, tree)
    names = [node["name"] for node in tree["children"]]
    check(names[0] == "Novel A" and "loose.epub" in names, names)
    folder = next(node for node in tree["children"] if node["name"] == "Novel A")
    check(folder["is_dir"] is True and folder["child_count"] == 2, folder)
    nested = next(node for node in folder["children"] if node["name"] == "extras")
    check(nested["children"][0]["name"] == "cover.jpg", nested)


def test_truncation_and_missing():
    root = tempfile.mkdtemp()
    for index in range(5):
        with open(os.path.join(root, f"{index}.epub"), "wb") as fh:
            fh.write(b"x")
    tree = scan_download_tree(root, max_entries=3)
    check(tree["truncated"] is True, tree)
    check(len(tree["children"]) == 3, tree)
    missing = scan_download_tree(os.path.join(root, "does-not-exist"))
    check("not available" in missing["error"], missing)


def main():
    test_nested_tree()
    test_truncation_and_missing()
    print("download inventory suite PASSED")


if __name__ == "__main__":
    main()
