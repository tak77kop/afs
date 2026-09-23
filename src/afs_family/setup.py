from setuptools import setup

package_name = 'afs_family'

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
    description='Family member node for AFS',
    license='TODO: License declaration',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'afs_family_member = afs_family.afs_family_member:main',
            'afs_generator = afs_family.afs_generator:main',
            'afs_member_evaluator = afs_family.afs_member_evaluator:main',
            'afs_document_processor = afs_family.afs_document_processor:main'
        ],
    },
)
