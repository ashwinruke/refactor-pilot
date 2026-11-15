#ifndef INVENTORY_PRICING_H
#define INVENTORY_PRICING_H

namespace inventory {

// Price for `quantity` units after bulk discounts.
double bulk_price(double unit_price, int quantity);

// Flat tax used before the 2014 pricing rework. Nothing calls this any more.
double legacy_flat_tax(double amount);

} // namespace inventory

#endif
