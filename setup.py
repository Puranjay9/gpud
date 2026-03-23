from setuptools import setup, find_packages

setup(
    name="gpud",
    version="0.1.0",
    description="Serverless GPU scaling daemon & CLI",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "pyyaml",
        "aiohttp",
        "docker",
        "nvidia-ml-py",
    ],
    entry_points={
        "console_scripts": [
            "gpud=gpud.cli:main",
        ],
    },
)
