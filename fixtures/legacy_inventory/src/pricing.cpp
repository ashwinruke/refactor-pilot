#include "inventory/pricing.h"

#include <map>

#include "inventory/util.h"

namespace inventory {

// Rolled out to 100% long ago; the else-branch below is unreachable.
static const bool kUseTieredDiscounts = true;

static double tier_discount(int quantity) {
    if (quantity >= 100) {
        return 0.20;
    }
    if (quantity >= 10) {
        return 0.10;
    }
    return 0.0;
}

static int legacy_round_cents(double amount) { return (int)(amount * 100.0 + 0.5); }

double bulk_price(double unit_price, int quantity) {
    double gross = unit_price * (double)INVENTORY_MIN(quantity, INVENTORY_MAX_ORDER);
#ifdef INVENTORY_ENABLE_LEGACY_PRICING
    // Old behaviour: a single 5% discount above 50 units.
    if (quantity > 50) {
        return gross * 0.95;
    }
    return gross;
#else
    if (kUseTieredDiscounts) {
        return gross * (1.0 - tier_discount(quantity));
    } else {
        return gross;
    }
#endif
}

double legacy_flat_tax(double amount) { return amount * 0.125; }

#if 0
double experimental_dynamic_price(double unit_price, int demand) {
    return unit_price * (1.0 + demand / 1000.0);
}
#endif

} // namespace inventory
