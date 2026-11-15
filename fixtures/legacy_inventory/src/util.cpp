#include "inventory/util.h"

namespace inventory {

int sum(const int* data, size_t count) {
    int total = 0;
    for (size_t i = 0; i < count; ++i) {
        total += data[i];
    }
    return total;
}

double variance(const double* data, size_t count) {
    if (count == 0) {
        return 0.0;
    }
    double mean = 0.0;
    for (size_t i = 0; i < count; ++i) {
        mean += data[i];
    }
    mean /= (double)count;
    double total = 0.0;
    for (size_t i = 0; i < count; ++i) {
        total += INVENTORY_SQUARE(data[i] - mean);
    }
    return total / (double)count;
}

bool has_prefix(const std::string& text, const std::string& prefix) {
    return text.size() >= prefix.size() && text.compare(0, prefix.size(), prefix) == 0;
}

std::string join(const std::vector<std::string>& parts, const char* separator) {
    std::string result;
    for (std::vector<std::string>::const_iterator it = parts.begin(); it != parts.end(); ++it) {
        if (it != parts.begin()) {
            result += separator;
        }
        result += *it;
    }
    return result;
}

} // namespace inventory
