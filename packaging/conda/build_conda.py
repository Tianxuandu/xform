# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.
import argparse
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

THIS_PATH = Path(__file__).resolve()
SOURCE_ROOT_DIR = str(THIS_PATH.parents[2])

PYTHON_VERSIONS = ["3.9", "3.10"]
PYTORCH_TO_CUDA_VERSIONS = {
    "1.11.0": ["10.2", "11.1", "11.3", "11.5"],
    "1.12.0": ["10.2", "11.3", "11.6"],
    "1.12.1": ["10.2", "11.3", "11.6"],
}


def conda_docker_image_for_cuda(cuda_version):
    """
    Given a cuda version, return a docker image we could
    build in.
    """

    if cuda_version in ("10.1", "10.2", "11.1"):
        return "pytorch/conda-cuda"
    if cuda_version == "11.3":
        return "pytorch/conda-builder:cuda113"
    if cuda_version == "11.5":
        return "pytorch/conda-builder:cuda115"
    if cuda_version == "11.6":
        return "pytorch/conda-builder:cuda116"
    raise ValueError(f"Unknown cuda version {cuda_version}")


def version_constraint(version):
    """
    Given version "11.3" returns " >=11.3,<11.4"
    """
    last_part = version.rindex(".") + 1
    upper = version[:last_part] + str(1 + int(version[last_part:]))
    return f" >={version},<{upper}"


@dataclass
class Build:
    """
    Represents one configuration of a build, i.e.
    a set of versions of dependent libraries.
    """

    python_version: str
    pytorch_version: str
    cuda_version: str

    conda_always_copy: bool = True
    conda_debug: bool = True

    def _set_env_for_build(self):
        if "CUDA_HOME" not in os.environ:
            if "FAIR_ENV_CLUSTER" in os.environ:
                cuda_home = "/public/apps/cuda/" + self.cuda_version
            else:
                # E.g. inside docker
                cuda_home = "/usr/local/cuda-" + self.cuda_version
            assert Path(cuda_home).is_dir
            os.environ["CUDA_HOME"] = cuda_home

        os.environ["TORCH_CUDA_ARCH_LIST"] = "6.0 7.0 7.5 8.0 8.6"
        os.environ["BUILD_VERSION"] = "dev"
        tag = subprocess.check_output(["git", "describe", "--tags"], text=True)
        os.environ["GIT_TAG"] = tag
        os.environ["PYTORCH_VERSION"] = self.pytorch_version
        os.environ["CU_VERSION"] = self.cuda_version
        os.environ["SOURCE_ROOT_DIR"] = SOURCE_ROOT_DIR
        os.environ["CONDA_CUDATOOLKIT_CONSTRAINT"] = version_constraint(
            self.cuda_version
        )
        os.environ["FORCE_CUDA"] = "1"

        if self.conda_always_copy:
            os.environ["CONDA_ALWAYS_COPY"] = "true"

    def _get_build_args(self):
        args = [
            "conda",
            "build",
            "-c",
            "fastchan",  # which can avoid needing pytorch and conda-forge
            "--no-anaconda-upload",
            "--python",
            self.python_version,
        ]
        if self.conda_debug:
            args += ["--debug"]
        args += ["--dirty"]
        args += ["--croot", "../build"]
        return args + ["packaging/conda/xformers"]

    def do_build(self):
        self._set_env_for_build()
        args = self._get_build_args()
        print(args)
        subprocess.check_call(args)

    def build_in_docker(self):
        filesystem = subprocess.check_output("stat -f -c %T .", shell=True)
        if filesystem in (b"nfs", b"tmpfs"):
            raise ValueError(
                "Cannot run docker here. "
                + "Please work on a local filesystem, e.g. /raid."
            )
        image = conda_docker_image_for_cuda(self.cuda_version)
        args = ["sudo", "docker", "run", "-it", "--rm", "-w", "/m"]
        args += ["-v", f"{SOURCE_ROOT_DIR}:/m", image]
        args += ["python3", str(THIS_PATH.relative_to(SOURCE_ROOT_DIR))]
        self_args = [
            "--cuda",
            self.cuda_version,
            "--pytorch",
            self.pytorch_version,
            "--python",
            self.python_version,
        ]
        args += self_args
        print(args)
        subprocess.check_call(args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the conda package.")
    parser.add_argument(
        "--python", metavar="3.X", required=True, help="python version e.g. 3.10"
    )
    parser.add_argument(
        "--cuda", metavar="1X.Y", required=True, help="cuda version e.g. 11.3"
    )
    parser.add_argument(
        "--pytorch", metavar="1.Y.Z", required=True, help="PyTorch version e.g. 1.11.0"
    )
    parser.add_argument(
        "--docker", action="store_true", help="Call this script inside docker."
    )

    args = parser.parse_args()

    pkg = Build(
        python_version=args.python, pytorch_version=args.pytorch, cuda_version=args.cuda
    )

    if args.docker:
        pkg.build_in_docker()
    else:
        pkg.do_build()


# python packaging/conda/build_conda.py  --cuda 11.6 --python 3.10 --pytorch 1.12.1
# python packaging/conda/build_conda.py  --cuda 11.3 --python 3.9 --pytorch 1.12.1  # <= the dino one
# python packaging/conda/build_conda.py  --cuda 11.6 --python 3.10 --pytorch 1.11.0

# Note this does the build outside the root of the tree.

# TODO:
# - Make a local conda package cache available inside docker
# - use ninja
# - do we need builds for both _GLIBCXX_USE_CXX11_ABI values?
# - how to prevent some cpu only builds of pytorch from being discovered?
