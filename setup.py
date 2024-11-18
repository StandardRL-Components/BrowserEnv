from setuptools import setup, find_packages

setup(
    name="browser_env",
    version="0.1",
    packages=find_packages(),
    include_package_data=True,  # Includes files specified in MANIFEST.in
    package_data={
        'browser_env': [
            'config-template/*',
            'extensions/*',
            'compose.yaml',
        ],
    },
    install_requires=[
        "gymnasium",
        "numpy",
        "vncdotool",
        "Pillow",
        "PyYAML",
        "docker",
        "websockets"
    ],
    description="A Gymnasium-compliant Browser environment.",
    author="Carlos Purves",
)