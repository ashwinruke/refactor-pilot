// Dependency-free test runner: `inventory_tests <suite>` exits non-zero on failure.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include <fstream>
#include <string>
#include <thread>
#include <vector>

#include "inventory/item.h"
#include "inventory/logger.h"
#include "inventory/pricing.h"
#include "inventory/util.h"
#include "inventory/warehouse.h"

using namespace inventory;

static int g_failures = 0;

#define CHECK(condition)                                                        \
    do {                                                                        \
        if (!(condition)) {                                                     \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #condition); \
            ++g_failures;                                                       \
        }                                                                       \
    } while (0)

static bool near(double a, double b) { return fabs(a - b) < 1e-9; }

static void test_item() {
    Item bolt(1, "bolt", 0.25, 400);
    CHECK(bolt.category() == CATEGORY_GENERIC);
    CHECK(near(bolt.total_value(), 100.0));
    CHECK(bolt.describe() == "#1 bolt x400 @ 0.25");

    PerishableItem milk(2, "milk", 2.0, 10, 1);
    CHECK(milk.category() == CATEGORY_PERISHABLE);
    CHECK(near(milk.total_value(), 10.0));
    CHECK(milk.describe() == "#2 milk x10 @ 2.00 (expires in 1 days)");

    PerishableItem cheese(3, "cheese", 5.0, 4, 30);
    CHECK(near(cheese.total_value(), 20.0));

    Item* made = create_item(CATEGORY_PERISHABLE, 4, "yoghurt", 1.5, 2);
    CHECK(made != NULL);
    CHECK(made->category() == CATEGORY_PERISHABLE);
    CHECK(near(made->total_value(), 3.0));
    delete made;
}

static void test_warehouse() {
    Warehouse warehouse("pune-1");
    CHECK(warehouse.name() == "pune-1");
    CHECK(warehouse.add_item(new Item(1, "bolt", 0.25, 400)) != NULL);
    CHECK(warehouse.add_item(new PerishableItem(2, "milk", 2.0, 10, 1)) != NULL);
    CHECK(warehouse.add_item(create_item(CATEGORY_FRAGILE, 3, "vase", 40.0, 2)) != NULL);
    CHECK(warehouse.size() == 3);

    // Duplicate id: rejected, and the warehouse disposes of the incoming item.
    CHECK(warehouse.add_item(new Item(1, "duplicate", 1.0, 1)) == NULL);
    CHECK(warehouse.add_item(NULL) == NULL);
    CHECK(warehouse.size() == 3);

    CHECK(warehouse.find(2) != NULL);
    CHECK(warehouse.find(2)->name() == "milk");
    CHECK(warehouse.find(99) == NULL);
    CHECK(near(warehouse.total_value(), 100.0 + 10.0 + 80.0));
    CHECK(warehouse.count_in_category(CATEGORY_GENERIC) == 2);
    CHECK(warehouse.count_in_category(CATEGORY_PERISHABLE) == 1);

    std::vector<const Item*> low = warehouse.low_stock(5);
    CHECK(low.size() == 1);
    CHECK(low[0]->id() == 3);

    warehouse.find(3)->set_quantity(50);
    CHECK(warehouse.low_stock(5).empty());

    CHECK(warehouse.remove(1));
    CHECK(!warehouse.remove(1));
    CHECK(warehouse.size() == 2);

    Item* released = warehouse.release(2);
    CHECK(released != NULL);
    CHECK(released->id() == 2);
    CHECK(warehouse.find(2) == NULL);
    CHECK(warehouse.release(2) == NULL);
    delete released;
    CHECK(warehouse.size() == 1);
}

static void write_lines(Logger* logger, int count) {
    for (int i = 0; i < count; ++i) {
        logger->log("INFO", "tick");
    }
}

static void test_logger() {
    const char* path = "inventory_test.log";
    {
        Logger logger(path);
        CHECK(logger.is_open());
        logger.log("WARN", "low stock");
        std::thread a(write_lines, &logger, 50);
        std::thread b(write_lines, &logger, 50);
        a.join();
        b.join();
        CHECK(logger.lines_written() == 101);
    }
    std::ifstream in(path);
    std::string line;
    int lines = 0;
    bool first_ok = false;
    while (std::getline(in, line)) {
        if (lines == 0) {
            first_ok = (line == "[WARN] low stock");
        }
        ++lines;
    }
    CHECK(first_ok);
    CHECK(lines == 101);
    remove(path);

    Logger closed(NULL);
    CHECK(!closed.is_open());
    closed.log("INFO", "dropped");
    CHECK(closed.lines_written() == 0);
}

static void test_pricing() {
    CHECK(near(bulk_price(2.0, 5), 10.0));
    CHECK(near(bulk_price(2.0, 10), 18.0));
    CHECK(near(bulk_price(2.0, 100), 160.0));
    CHECK(near(bulk_price(1.0, 20000), 8000.0));
}

static void test_util() {
    int values[] = {1, 2, 3, 4};
    CHECK(sum(values, 4) == 10);
    CHECK(sum(values, 0) == 0);

    double samples[] = {2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0};
    CHECK(near(variance(samples, 8), 4.0));
    CHECK(near(variance(samples, 0), 0.0));

    CHECK(has_prefix("perishable-milk", "perishable"));
    CHECK(!has_prefix("milk", "perishable"));
    CHECK(has_prefix("milk", ""));

    std::vector<std::string> parts;
    parts.push_back("a");
    parts.push_back("b");
    parts.push_back("c");
    CHECK(join(parts, ", ") == "a, b, c");
    CHECK(join(std::vector<std::string>(), ", ").empty());

    CHECK(clamp_non_negative(-3) == 0);
    CHECK(clamp_non_negative(7L) == 7L);
    CHECK(near(clamp_non_negative(-0.5), 0.0));
    CHECK(near(clamp_non_negative(2.5f), 2.5));
    CHECK(INVENTORY_SQUARE(3) == 9);
    CHECK(INVENTORY_MIN(4, 9) == 4);
}

int main(int argc, char** argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s <item|warehouse|logger|pricing|util>\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "item") == 0) {
        test_item();
    } else if (strcmp(argv[1], "warehouse") == 0) {
        test_warehouse();
    } else if (strcmp(argv[1], "logger") == 0) {
        test_logger();
    } else if (strcmp(argv[1], "pricing") == 0) {
        test_pricing();
    } else if (strcmp(argv[1], "util") == 0) {
        test_util();
    } else {
        fprintf(stderr, "unknown suite: %s\n", argv[1]);
        return 2;
    }
    if (g_failures != 0) {
        fprintf(stderr, "%d check(s) failed\n", g_failures);
        return 1;
    }
    return 0;
}
