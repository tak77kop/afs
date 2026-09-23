from setuptools import setup

package_name = 'afs_therapist'

setup(
    name=package_name,
    version='0.0.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='ubuntu',
    maintainer_email='ubuntu@todo.todo',
    description='Therapist node for AFS',
    license='TODO: License declaration',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'afs_therapist = afs_therapist.afs_therapist:main',
            'afs_evaluator = afs_therapist.afs_evaluator:main',
            'afs_optimizer = afs_therapist.afs_optimizer:main'
        ],
    },
)
