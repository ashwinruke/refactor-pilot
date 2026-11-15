cmake_minimum_required(VERSION 3.16)
project(legacy_inventory CXX)

# Deliberately old: this fixture is the "before" state for refactor-pilot.
set(CMAKE_CXX_STANDARD 11 CACHE STRING "C++ standard")
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Threads REQUIRED)

add_library(inventory
  src/item.cpp
  src/warehouse.cpp
  src/logger.cpp
  src/pricing.cpp
  src/util.cpp
)
target_include_directories(inventory PUBLIC include)
target_link_libraries(inventory PUBLIC Threads::Threads)

enable_testing()
add_executable(inventory_tests tests/test_main.cpp)
target_link_libraries(inventory_tests PRIVATE inventory)
foreach(suite item warehouse logger pricing util)
  add_test(NAME ${suite} COMMAND inventory_tests ${suite})
endforeach()
