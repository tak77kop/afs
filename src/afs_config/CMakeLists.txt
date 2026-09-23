cmake_minimum_required(VERSION 3.8)
project(afs_config)

find_package(ament_cmake REQUIRED)

install(DIRECTORY config
    DESTINATION share/afs_config
)

ament_package()
