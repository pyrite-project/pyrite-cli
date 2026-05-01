from setuptools import setup, find_packages

packages = find_packages()

setup(
    name='pyrite-cli',
    version='0.0.1',
    packages=packages.append('cli'),
    entry_points={
        'console_scripts': [
            'pyrcli=cli.main:main', 
        ],
    },
    install_requires=[],
    python_requires='>=3.10',
)