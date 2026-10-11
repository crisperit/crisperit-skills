def greet(name, excited=False):
    suffix = "!" if excited else "."
    return f"Hello, {name}{suffix}"


def total(items):
    return sum(i["price"] * i.get("qty", 1) for i in items)


if __name__ == "__main__":
    print(greet("walkthrough", excited=True))
    print(total([{"price": 2.5, "qty": 2}, {"price": 1}]))
