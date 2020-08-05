from setuptools import setup

setup(
    name="pyTibber",
    packages=["tibber"],
    install_requires=[
        "aiohttp>=3.0.6",
        "async_timeout>=1.4.0",
        "websockets>=6.0",
        "graphql-subscription-manager>=0.3.6",
        "pytz",
        "python-dateutil",
    ],
    version="0.15.2",
    description="A python3 library to communicate with Tibber",
    python_requires=">=3.5.3",
    author="Daniel Hoyer",
    author_email="mail@dahoiv.net",
    url="https://github.com/Danielhiversen/pyTibber",
    license="MIT",
    classifiers=[
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Topic :: Home Automation",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
