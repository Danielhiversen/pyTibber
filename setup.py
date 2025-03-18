from pathlib import Path
from typing import Any

from setuptools import setup

consts: dict[str, Any] = {}
exec((Path("tibber") / "const.py").read_text(encoding="utf-8"), consts)  # noqa: S102

setup(
    name="pyTibber",
    packages=["tibber"],
    install_requires=[
        "aiohttp>=3.11.13",
        "gql[aiohttp,websockets]>=3.5.0",
        "websockets>=11.0",
    ],
    package_data={"tibber": ["py.typed"]},
    version=consts["__version__"],
    description="A python3 library to communicate with Tibber",
    python_requires=">=3.11.0",
    author="Daniel Hjelseth Hoyer",
    author_email="mail@dahoiv.net",
    url="https://github.com/Danielhiversen/pyTibber",
    classifiers=[
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Topic :: Home Automation",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
    ],
)
