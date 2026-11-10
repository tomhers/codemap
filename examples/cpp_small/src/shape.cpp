#include "geo/shape.h"
#include "util.h"

namespace geo {

double Shape::scaled(double k) const { return k * area(); }

double Shape::scaled(double k, double offset) const {
    return scaled(k) + offset;
}

int Shape::id() const { return id_; }
int &Shape::id() { return id_; }

Circle::Circle(double r) : r_(r) {}

double Circle::area() const { return util::pi() * r_ * r_; }

double total_area(const std::vector<Shape*> &shapes) {
    double sum = 0;
    for (auto *s : shapes) sum += s->area();
    return sum;
}

} // namespace geo

int main() {
    geo::Circle c(2.0);
    std::vector<geo::Shape*> v{&c};
    return geo::total_area(v) > 0 && geo::traits<geo::Circle>::name() ? 0 : 1;
}
