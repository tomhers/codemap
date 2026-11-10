#pragma once
#include <vector>

namespace geo {

/// Anything with an area.
class Shape {
public:
    virtual ~Shape() = default;
    /// Area in square units.
    virtual double area() const = 0;
    double scaled(double k) const;
    double scaled(double k, double offset) const;
    int id() const;
    int &id();
private:
    int id_ = 0;
};

class Circle : public Shape {
public:
    explicit Circle(double r);
    double area() const override;
private:
    double r_;
};

template <class T> struct traits;

template <> struct traits<Circle> {
    static const char *name() { return "circle"; }
};

double total_area(const std::vector<Shape*> &shapes);

} // namespace geo
