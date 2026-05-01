from setuptools import setup, find_packages

setup(
    name='pyrite-cli',
    version='0.0.1',
    packages=find_packages().append('cli'),
    entry_points={
        'console_scripts': [
            'pyrite-cli=cli.main:main', 
        ],
    },
    install_requires=[],
    python_requires='>=3.10',
)