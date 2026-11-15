#ifndef INVENTORY_LOGGER_H
#define INVENTORY_LOGGER_H

#include <stdio.h>

#include <mutex>
#include <string>

namespace inventory {

// Appends lines to a file. Safe to call from several threads.
class Logger {
public:
    explicit Logger(const char* path);
    ~Logger();

    bool is_open() const;
    void log(const char* level, const std::string& message);
    int lines_written() const;

private:
    Logger(const Logger&);
    Logger& operator=(const Logger&);

    FILE* file_;
    mutable std::mutex mutex_;
    int lines_;
};

} // namespace inventory

#endif
