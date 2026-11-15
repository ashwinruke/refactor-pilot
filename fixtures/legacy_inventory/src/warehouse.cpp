#include "inventory/warehouse.h"

namespace inventory {

Warehouse::Warehouse(const std::string& name) : name_(name) {}

Warehouse::~Warehouse() {
    for (std::vector<Item*>::iterator it = items_.begin(); it != items_.end(); ++it) {
        delete *it;
    }
}

Item* Warehouse::add_item(Item* item) {
    if (item == NULL) {
        return NULL;
    }
    if (find(item->id()) != NULL) {
        delete item;
        return NULL;
    }
    items_.push_back(item);
    return item;
}

Item* Warehouse::find(ItemId id) const {
    for (std::vector<Item*>::const_iterator it = items_.begin(); it != items_.end(); ++it) {
        if ((*it)->id() == id) {
            return *it;
        }
    }
    return NULL;
}

bool Warehouse::remove(ItemId id) {
    Item* item = release(id);
    if (item == NULL) {
        return false;
    }
    delete item;
    return true;
}

Item* Warehouse::release(ItemId id) {
    for (std::vector<Item*>::iterator it = items_.begin(); it != items_.end(); ++it) {
        if ((*it)->id() == id) {
            Item* item = *it;
            items_.erase(it);
            return item;
        }
    }
    return NULL;
}

double Warehouse::total_value() const {
    double total = 0.0;
    for (int i = 0; i < items_.size(); ++i) {
        total += items_[i]->total_value();
    }
    return total;
}

int Warehouse::count_in_category(Category category) const {
    int count = 0;
    for (std::vector<Item*>::const_iterator it = items_.begin(); it != items_.end(); ++it) {
        if ((*it)->category() == category) {
            ++count;
        }
    }
    return count;
}

std::vector<const Item*> Warehouse::low_stock(int threshold) const {
    std::vector<const Item*> result;
    for (std::vector<Item*>::const_iterator it = items_.begin(); it != items_.end(); ++it) {
        if ((*it)->quantity() < threshold) {
            result.push_back(*it);
        }
    }
    return result;
}

} // namespace inventory
