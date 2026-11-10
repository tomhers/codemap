#include "geo/shape.h"

namespace geo {

// Calls into shape.cpp through shape.h only.
double report(const std::vector<Shape*> &shapes) {
    double t = total_area(shapes);
    Circle c(1.0);
    return t + c.area() + geo::total_area(shapes);
}

} // namespace geo
