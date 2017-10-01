from setuptools import setup

setup(
    name = 'pyTibber',
    packages = ['Tibber'],
    install_requires=['gql==0.1.0', 'aiohttp==2.2.5', 'async_timeout==1.4.0'],
    version = '0.1.1',
    description = 'a library to communicate with Tibber',
    author='Daniel Hoyer Iversen',
    author_email='mail@dahoiv.net',
    url='https://github.com/Danielhiversen/pyRFXtrx',
    classifiers=[
        'Intended Audience :: Developers',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Topic :: Home Automation',
        'Topic :: Software Development :: Libraries :: Python Modules'
        ]
)
