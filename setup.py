from setuptools import setup, find_packages
from os.path import join, dirname

setup(
    name='interests_fetcher',
    version='0.0.1',
    packages=find_packages(),
    author='punchyarchy',
    author_email='ksmdrmvscthny@gmail.com',
    long_description=open(join(dirname(__file__), 'readme.txt')).read(),
    install_requires=open(join(dirname(__file__),
                               'requirements.txt')).readlines()
)
