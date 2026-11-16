class SimulatedCrash(Exception):
    pass


class CrashInjector:
    def __init__(self, *points):
        self.points = set(points)

    def hit(self, point):
        if point in self.points:
            self.points.remove(point)
            raise SimulatedCrash(point)
