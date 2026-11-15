#include "inventory/logger.h"

namespace inventory {

Logger::Logger(const char* path) : file_(NULL), lines_(0) {
    if (path != NULL) {
        file_ = fopen(path, "w");
    }
}

Logger::~Logger() {
    if (file_ != NULL) {
        fclose(file_);
    }
}

bool Logger::is_open() const { return file_ != NULL; }

void Logger::log(const char* level, const std::string& message) {
    mutex_.lock();
    if (file_ == NULL) {
        mutex_.unlock();
        return;
    }
    fprintf(file_, "[%s] %s\n", level, message.c_str());
    fflush(file_);
    ++lines_;
    mutex_.unlock();
}

int Logger::lines_written() const {
    mutex_.lock();
    int lines = lines_;
    mutex_.unlock();
    return lines;
}

} // namespace inventory
