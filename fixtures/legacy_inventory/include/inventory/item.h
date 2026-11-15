#ifndef INVENTORY_ITEM_H
#define INVENTORY_ITEM_H

#include <string>

namespace inventory {

typedef unsigned int ItemId;

enum Category { CATEGORY_GENERIC, CATEGORY_PERISHABLE, CATEGORY_FRAGILE };

class Item {
public:
    Item(ItemId id, const std::string& name, double unit_price, int quantity);
    virtual ~Item();

    ItemId id() const { return id_; }
    const std::string& name() const { return name_; }
    double unit_price() const { return unit_price_; }
    int quantity() const { return quantity_; }
    void set_quantity(int quantity) { quantity_ = quantity; }

    virtual Category category() const;
    virtual double total_value() const;
    virtual std::string describe() const;

private:
    ItemId id_;
    std::string name_;
    double unit_price_;
    int quantity_;
};

class PerishableItem : public Item {
public:
    PerishableItem(ItemId id, const std::string& name, double unit_price, int quantity, int days_to_expiry);
    virtual ~PerishableItem();

    int days_to_expiry() const { return days_to_expiry_; }

    virtual Category category() const;
    virtual double total_value() const;
    virtual std::string describe() const;

private:
    int days_to_expiry_;
};

// Returns a heap-allocated item. The caller owns it and must delete it
// (or hand it to a Warehouse, which then owns it).
// Returns NULL for an unknown category.
Item* create_item(Category category, ItemId id, const std::string& name, double unit_price, int quantity);

} // namespace inventory

#endif
