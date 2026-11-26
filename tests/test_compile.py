import os
import pathlib
import py_compile
import re
import shutil
import subprocess

import pytest

import repo

PY_FILES = repo.py_files()
EMBEDDED = [s for s in repo.sh_files() if "<<'PY'" in s.read_text()]
PY_HEREDOC = re.compile(r"<<'PY'\n(.*?)\nPY\n", re.S)
assert PY_FILES and EMBEDDED


@pytest.mark.parametrize("path", PY_FILES, ids=[repo.rel(p) for p in PY_FILES])
def test_python_compiles(path, tmp_path):
    py_compile.compile(str(path), cfile=str(tmp_path / "out.pyc"), doraise=True)


@pytest.mark.parametrize("script", EMBEDDED, ids=[repo.rel(p) for p in EMBEDDED])
def test_embedded_python_compiles(script):
    for n, block in enumerate(PY_HEREDOC.findall(script.read_text())):
        compile(block, "%s:PY%d" % (repo.rel(script), n), "exec")


# Everything that has to keep working after boot is a module of its own, so this is the
# place that says whether they compile at all: neither one is tested against hardware
# here, and a card that would have taken the change is not the same claim as a tree that
# builds.  Each is built from a copy, in a directory of its own, because the second one
# has to prove its own Makefile's header probe works and not inherit the first one's
# object files.
COMPANIONS = {"passthrough": "cmp_no_bus_reset", "hbm-pm": "cmp_hbm_pm"}


@pytest.mark.parametrize("directory,module", sorted(COMPANIONS.items()))
def test_companion_module_builds(directory, module, tmp_path):
    builds = [b for b in pathlib.Path("/lib/modules").glob("*/build") if b.is_dir()]
    running = pathlib.Path("/lib/modules", os.uname().release, "build")
    build = running if running.is_dir() else (builds[-1] if builds else None)
    assert build, "no kernel headers under /lib/modules/*/build"
    for name in (module + ".c", "Makefile"):
        shutil.copy(repo.ROOT / "driver" / directory / name, tmp_path / name)
    r = subprocess.run(["make", "-C", str(tmp_path), "KVER=" + build.parent.name],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / (module + ".ko")).is_file()
