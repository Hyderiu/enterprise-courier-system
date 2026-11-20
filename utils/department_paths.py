"""Helpers for displaying and storing complete department hierarchy paths."""


def build_department_path_map(departments, separator="-"):
    departments = list(departments)
    by_id = {department.id: department for department in departments}
    cache = {}

    def resolve(department, visiting=None):
        if department.id in cache:
            return cache[department.id]
        visiting = set(visiting or ())
        if department.id in visiting:
            return department.dept_name
        visiting.add(department.id)
        parent = by_id.get(department.parent_id)
        if parent:
            value = f"{resolve(parent, visiting)}{separator}{department.dept_name}"
        else:
            value = department.dept_name
        cache[department.id] = value
        return value

    return {department.id: resolve(department) for department in departments}
