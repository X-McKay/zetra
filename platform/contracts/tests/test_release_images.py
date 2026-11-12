"""Which platform images a push releases (`agent_platform/images.py`), on throwaway repositories."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_platform import images as tool
from agent_platform.images import ImagesError

ZERO = "0" * 40


def entry(name: str, context: str, dockerfile: str | None = None, **extra) -> dict:
    return {
        "repository": name,
        "dockerfile": dockerfile or f"{context}/Dockerfile",
        "context": context,
        "platform": "linux/amd64",
        "buildArgs": {},
        **extra,
    }


DEFAULT = [
    entry("alpha", "platform/alpha"),
    entry("beta", "platform/beta"),
    entry("gamma", "platform/shared"),  # gamma and delta share one context
    entry("delta", "platform/shared", "platform/shared/Dockerfile.delta"),
    entry("wide", "platform", "platform/wide/Dockerfile"),  # parent of every other context
]


class Repo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def write(self, rel: str, text: str = "x") -> None:
        file = self.path / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def set_images(self, images: list[dict]) -> None:
        self.write("platform/images.json", json.dumps(images, indent=2))

    def commit(self, message: str = "c") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def released(self, before: str, after: str) -> list[str]:
        return [i["repository"] for i in tool.changed(self.path, before, after).images]


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path / "r")
    r.set_images(DEFAULT)
    for rel in (
        "platform/alpha/Dockerfile",
        "platform/alpha/main.go",
        "platform/beta/Dockerfile",
        "platform/shared/Dockerfile",
        "platform/shared/Dockerfile.delta",
        "platform/shared/lib.py",
        "platform/wide/Dockerfile",
        "docs/readme.md",
    ):
        r.write(rel)
    r.commit("base")
    return r


def head(r: Repo) -> str:
    return r.git("rev-parse", "HEAD")


def test_change_to_one_context_releases_only_that_image(repo):
    base = head(repo)
    repo.write("platform/beta/new.txt")
    # wide's context is `platform`, so it is a build input of wide as well; nothing else is.
    assert repo.released(base, repo.commit()) == ["beta", "wide"]


def test_change_outside_every_context_releases_nothing(repo):
    base = head(repo)
    repo.write("docs/readme.md", "changed")
    selection = tool.changed(repo.path, base, repo.commit())
    assert selection.images == []
    assert "no image has changed build inputs" in selection.notes


def test_dockerfile_outside_the_context_directory_is_still_an_input(repo):
    repo.set_images([entry("odd", "platform/alpha", "platform/odd/Dockerfile")])
    repo.write("platform/odd/Dockerfile")
    base = repo.commit()
    repo.write("platform/odd/Dockerfile", "changed")
    assert repo.released(base, repo.commit()) == ["odd"]


def test_file_shared_by_two_contexts_releases_both(repo):
    base = head(repo)
    repo.write("platform/shared/lib.py", "changed")
    assert repo.released(base, repo.commit()) == ["gamma", "delta", "wide"]


def test_a_directory_name_prefix_is_not_a_context(repo):
    base = head(repo)
    repo.write("platform/alpha-extra/file")  # shares the text "platform/alpha"
    assert repo.released(base, repo.commit()) == ["wide"]


def test_parent_context_does_not_pull_in_the_children(repo):
    base = head(repo)
    repo.write("platform/wide/Dockerfile", "changed")
    assert repo.released(base, repo.commit()) == ["wide"]


def test_root_context_is_changed_by_any_file(repo):
    repo.set_images([entry("root", ".", "Dockerfile"), entry("beta", "platform/beta")])
    repo.write("Dockerfile")
    base = repo.commit()
    repo.write("docs/readme.md", "changed")
    assert repo.released(base, repo.commit()) == ["root"]


def test_images_json_edit_alone_does_not_rebuild_the_parent_context(repo):
    base = head(repo)
    edited = [dict(i) for i in DEFAULT]
    edited[1]["platform"] = "linux/arm64"
    repo.set_images(edited)
    # Only beta's entry changed. `wide` has context `platform`, which holds images.json,
    # but its own entry is unchanged and no Dockerfile copies the list.
    assert repo.released(base, repo.commit()) == ["beta"]


def test_added_entry_is_released_and_nothing_else(repo):
    base = head(repo)
    repo.write("platform/new/Dockerfile")
    repo.set_images([*DEFAULT, entry("new", "platform/new")])
    assert repo.released(base, repo.commit()) == [
        "wide",
        "new",
    ]  # wide: new/Dockerfile is in platform


def test_added_entry_for_existing_files_releases_only_the_entry(repo):
    base = head(repo)
    repo.set_images([*DEFAULT, entry("alpha-arm", "platform/alpha")])
    assert repo.released(base, repo.commit()) == ["alpha-arm"]


def test_removed_entry_is_not_listed_and_is_explained(repo):
    base = head(repo)
    repo.set_images([i for i in DEFAULT if i["repository"] != "beta"])
    selection = tool.changed(repo.path, base, repo.commit())
    assert selection.images == []
    assert any("beta: removed" in n for n in selection.notes)


def test_edited_build_argument_releases_that_entry(repo):
    base = head(repo)
    edited = [dict(i) for i in DEFAULT]
    edited[0]["buildArgs"] = {"V": "2"}
    repo.set_images(edited)
    assert repo.released(base, repo.commit()) == ["alpha"]


def test_reordering_entries_releases_nothing(repo):
    base = head(repo)
    repo.set_images(list(reversed(DEFAULT)))
    assert repo.released(base, repo.commit()) == []


def test_deleted_file_counts(repo):
    base = head(repo)
    (repo.path / "platform/alpha/main.go").unlink()
    assert repo.released(base, repo.commit()) == ["alpha", "wide"]


def test_deleted_dockerfile_counts(repo):
    base = head(repo)
    (repo.path / "platform/beta/Dockerfile").unlink()
    assert "beta" in repo.released(base, repo.commit())


def test_rename_counts_for_the_old_and_the_new_location(repo):
    base = head(repo)
    repo.git("mv", "platform/alpha/main.go", "platform/beta/main.go")
    # The same bytes under a new path: both contexts change.
    assert repo.released(base, repo.commit()) == ["alpha", "beta", "wide"]


def test_rename_out_of_all_contexts_still_releases_the_old_owner(repo):
    base = head(repo)
    repo.git("mv", "platform/alpha/main.go", "docs/main.go")
    assert repo.released(base, repo.commit()) == ["alpha", "wide"]


def test_merge_commit_gives_the_net_change_of_the_push(repo):
    base = head(repo)
    repo.git("checkout", "-q", "-b", "side")
    repo.write("platform/alpha/side.txt")
    repo.commit("side")
    repo.git("checkout", "-q", "main")
    repo.write("docs/other.md")
    repo.commit("main moved")
    repo.git("merge", "-q", "--no-ff", "-m", "merge", "side")
    after = head(repo)
    # The push moved main from `base` to the merge: alpha's side change arrives with it.
    assert repo.released(base, after) == ["alpha", "wide"]
    # Merge into main as seen from main's previous tip: only the side branch's change.
    first_parent = repo.git("rev-parse", f"{after}^1")
    assert repo.released(first_parent, after) == ["alpha", "wide"]


def test_push_of_several_commits_that_cancel_out_releases_nothing(repo):
    base = head(repo)
    repo.write("platform/beta/tmp.txt")
    repo.commit()
    (repo.path / "platform/beta/tmp.txt").unlink()
    assert repo.released(base, repo.commit()) == []


def test_unchanged_image_is_never_listed_when_other_images_change(repo):
    base = head(repo)
    repo.write("platform/alpha/main.go", "changed")
    names = repo.released(base, repo.commit())
    assert "beta" not in names and "gamma" not in names and "delta" not in names


def test_all_zero_before_releases_nothing_and_says_why(repo):
    selection = tool.changed(repo.path, ZERO, head(repo))
    assert selection.images == []
    assert "all zeros" in selection.notes[0]


def test_before_commit_unknown_to_the_checkout_releases_nothing(repo):
    selection = tool.changed(repo.path, "1234567890abcdef1234567890abcdef12345678", head(repo))
    assert selection.images == []
    assert "not in this checkout" in selection.notes[0]


def test_unknown_after_commit_is_an_error(repo):
    with pytest.raises(ImagesError):
        tool.changed(repo.path, head(repo), "1234567890abcdef1234567890abcdef12345678")


def test_before_commit_without_an_images_file_releases_every_entry(repo):
    repo.git("rm", "-q", "platform/images.json")
    base = repo.commit("no list")
    repo.set_images(DEFAULT)
    assert repo.released(base, repo.commit()) == [i["repository"] for i in DEFAULT]


def test_malformed_images_file_stops_the_run(repo):
    base = head(repo)
    repo.write("platform/images.json", "{not json")
    with pytest.raises(ImagesError):
        tool.changed(repo.path, base, repo.commit())


def test_duplicate_or_incomplete_entries_are_rejected():
    with pytest.raises(ImagesError):
        tool.parse_images(json.dumps([entry("a", "x"), entry("a", "y")]))
    with pytest.raises(ImagesError):
        tool.parse_images(json.dumps([{"repository": "a"}]))
    with pytest.raises(ImagesError):
        tool.parse_images(json.dumps([entry("../a", "x")]))


def test_cli_prints_a_json_array_and_reasons_on_stderr(repo, capsys):
    base = head(repo)
    repo.write("platform/alpha/main.go", "changed")
    after = repo.commit()
    assert tool.main(["changed", "--repo", str(repo.path), base, after]) == 0
    out = capsys.readouterr()
    assert [i["repository"] for i in json.loads(out.out)] == ["alpha", "wide"]
    assert "release alpha: Dockerfile or build context changed" in out.err


def test_cli_prints_an_empty_array_when_nothing_changed(repo, capsys):
    assert tool.main(["changed", "--repo", str(repo.path), head(repo), head(repo)]) == 0
    assert capsys.readouterr().out.strip() == "[]"


def test_real_images_file_entries_resolve():
    repo = Path(__file__).parents[3]
    names = [i["repository"] for i in tool.load_images(repo)]
    assert "budget-backend" in names
    fields = tool.entry_fields(repo, "registry-controller")
    assert fields[:3] == ["platform/controller/Dockerfile", "platform", "linux/amd64"]
    assert any(f.startswith("COSIGN_VERSION=") for f in fields[3:])
    with pytest.raises(ImagesError):
        tool.entry_fields(repo, "no-such-image")


def test_metadata_digest():
    good = "sha256:" + "ab" * 32
    assert tool.metadata_digest(json.dumps({"containerimage.digest": good})) == good
    for bad in ("{}", "[]", "x", json.dumps({"containerimage.digest": "sha256:zz"})):
        with pytest.raises(ImagesError):
            tool.metadata_digest(bad)


@pytest.mark.parametrize(
    ("config", "present"),
    [
        ({"auths": {"registry.almckay.io": {"auth": "eA=="}}}, True),
        ({"auths": {"https://registry.almckay.io/v1/": {"auth": "eA=="}}}, True),
        ({"credHelpers": {"registry.almckay.io": "pass"}}, True),
        ({"auths": {"registry.almckay.io": {}}}, False),
        ({"auths": {"registry.almckay.io.evil.example": {"auth": "eA=="}}}, False),
        ({"auths": {"other.example": {"auth": "eA=="}}}, False),
        ({}, False),
        ([], False),
    ],
)
def test_registry_auth_present(config, present):
    assert tool.registry_auth_present(json.dumps(config), "registry.almckay.io") is present


def test_registry_auth_present_rejects_unparsable_text():
    assert tool.registry_auth_present("not json", "registry.almckay.io") is False


def test_declared_inputs_replace_the_context_for_change_detection(repo):
    narrow = [dict(i) for i in DEFAULT]
    narrow[4]["inputs"] = ["platform/wide/src", "platform/alpha/main.go"]
    repo.set_images(narrow)
    repo.write("platform/wide/src/a.py")
    base = repo.commit()
    repo.write("platform/beta/other.txt")  # inside context `platform`, outside the inputs
    assert repo.released(base, repo.commit()) == ["beta"]
    repo.write("platform/wide/src/a.py", "changed")
    assert repo.released(base, repo.commit()) == ["beta", "wide"]


def test_declared_inputs_still_release_on_dockerfile_and_entry_changes(repo):
    narrow = [dict(i) for i in DEFAULT]
    narrow[4]["inputs"] = ["platform/wide/src"]
    repo.set_images(narrow)
    base = repo.commit()
    repo.write("platform/wide/Dockerfile", "changed")
    mid = repo.commit()
    assert repo.released(base, mid) == ["wide"]
    narrow[4]["inputs"] = ["platform/wide/src", "platform/alpha"]
    repo.set_images(narrow)
    assert repo.released(mid, repo.commit()) == ["wide"]


def test_a_deleted_input_counts(repo):
    narrow = [dict(i) for i in DEFAULT]
    narrow[4]["inputs"] = ["platform/wide/src"]
    repo.set_images(narrow)
    repo.write("platform/wide/src/a.py")
    base = repo.commit()
    (repo.path / "platform/wide/src/a.py").unlink()
    assert repo.released(base, repo.commit()) == ["wide"]


def test_malformed_inputs_are_rejected():
    for bad in ([], "platform/a", [""], ["../x"], [1]):
        with pytest.raises(ImagesError):
            tool.parse_images(json.dumps([entry("a", "x", inputs=bad)]))
