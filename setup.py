import os

from setuptools import setup

consts = {}
with open(os.path.join("tibber", "const.py")) as fp:
    exec(fp.read(), consts)

setup(
    name="pyTibber",
    packages=["tibber"],
    install_requires=[
        "aiohttp>=3.0.6",
        "async_timeout>=1.4.0",
        "gql[aiohttp,websockets]",
        "python-dateutil",
    ],
    package_data={"tibber": ["py.typed"]},
    version=consts["__version__"],
    description="A python3 library to communicate with Tibber",
    python_requires=">=3.9.0",
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
    ],
)
