#ifndef INVENTORY_UTIL_H
#define INVENTORY_UTIL_H

#include <stddef.h>
#include <string>
#include <type_traits>
#include <vector>

#define INVENTORY_MAX_DESCRIPTION 160
#define INVENTORY_MAX_ORDER 10000
#define INVENTORY_SQUARE(x) ((x) * (x))
#define INVENTORY_MIN(a, b) ((a) < (b) ? (a) : (b))

namespace inventory {

// Sum of `count` ints starting at `data`.
int sum(const int* data, size_t count);

// Mean of squared deviations; 0 for an empty range.
double variance(const double* data, size_t count);

bool has_prefix(const std::string& text, const std::string& prefix);

std::string join(const std::vector<std::string>& parts, const char* separator);

// Negative values become zero. Integers and floating point only.
template <typename T>
typename std::enable_if<std::is_integral<T>::value, T>::type clamp_non_negative(T value) {
    return value < 0 ? 0 : value;
}

template <typename T>
typename std::enable_if<std::is_floating_point<T>::value, T>::type clamp_non_negative(T value) {
    return value < T(0) ? T(0) : value;
}

} // namespace inventory

#endif
