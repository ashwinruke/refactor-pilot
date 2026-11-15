#include "inventory/item.h"

#include <stdio.h>

#include "inventory/util.h"

namespace inventory {

Item::Item(ItemId id, const std::string& name, double unit_price, int quantity)
    : id_(id), name_(name), unit_price_(unit_price), quantity_(quantity) {}

Item::~Item() {}

Category Item::category() const { return CATEGORY_GENERIC; }

double Item::total_value() const { return unit_price_ * (double)quantity_; }

std::string Item::describe() const {
    char buffer[INVENTORY_MAX_DESCRIPTION];
    snprintf(buffer, sizeof(buffer), "#%u %s x%d @ %.2f", id_, name_.c_str(), quantity_, unit_price_);
    return std::string(buffer);
}

PerishableItem::PerishableItem(ItemId id, const std::string& name, double unit_price, int quantity,
                               int days_to_expiry)
    : Item(id, name, unit_price, quantity), days_to_expiry_(days_to_expiry) {}

PerishableItem::~PerishableItem() {}

Category PerishableItem::category() const { return CATEGORY_PERISHABLE; }

double PerishableItem::total_value() const {
    double value = Item::total_value();
    if (days_to_expiry_ <= 2) {
        value = value * 0.5;
    }
    return value;
}

std::string PerishableItem::describe() const {
    char buffer[INVENTORY_MAX_DESCRIPTION];
    snprintf(buffer, sizeof(buffer), "%s (expires in %d days)", Item::describe().c_str(), days_to_expiry_);
    return std::string(buffer);
}

Item* create_item(Category category, ItemId id, const std::string& name, double unit_price, int quantity) {
    Item* item = NULL;
    switch (category) {
    case CATEGORY_GENERIC:
    case CATEGORY_FRAGILE:
        item = new Item(id, name, unit_price, quantity);
        break;
    case CATEGORY_PERISHABLE:
        item = new PerishableItem(id, name, unit_price, quantity, 7);
        break;
    }
    return item;
}

} // namespace inventory
