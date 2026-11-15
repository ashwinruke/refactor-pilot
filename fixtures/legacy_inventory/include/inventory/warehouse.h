#ifndef INVENTORY_WAREHOUSE_H
#define INVENTORY_WAREHOUSE_H

#include <stddef.h>
#include <string>
#include <vector>

#include "inventory/item.h"

namespace inventory {

class Warehouse {
public:
    explicit Warehouse(const std::string& name);
    ~Warehouse();

    const std::string& name() const { return name_; }
    size_t size() const { return items_.size(); }

    // Takes ownership of `item`. If an item with the same id already exists,
    // the incoming item is deleted and NULL is returned.
    Item* add_item(Item* item);

    // Non-owning lookup. Returns NULL when the id is unknown.
    Item* find(ItemId id) const;

    // Removes and deletes the item. Returns false when the id is unknown.
    bool remove(ItemId id);

    // Removes the item and hands ownership back to the caller, or NULL.
    Item* release(ItemId id);

    double total_value() const;
    int count_in_category(Category category) const;

    // Non-owning pointers into the warehouse; invalidated by remove/release.
    std::vector<const Item*> low_stock(int threshold) const;

private:
    // Not copyable: declared private and never defined.
    Warehouse(const Warehouse&);
    Warehouse& operator=(const Warehouse&);

    std::string name_;
    std::vector<Item*> items_;
};

} // namespace inventory

#endif
