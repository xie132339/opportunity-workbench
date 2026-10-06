"""Group related unittest scenario checks under named subtests."""

def grouped_scenarios(groups):
    def decorate(test_case):
        for test_name, scenario_names in groups.items():
            scenarios = tuple((name, getattr(test_case, f"_case_{name}")) for name in scenario_names)

            def run_group(self, _scenarios=scenarios):
                for name, scenario in _scenarios:
                    with self.subTest(scenario=name):
                        scenario(self)

            run_group.__name__ = test_name
            run_group.__qualname__ = f"{test_case.__qualname__}.{test_name}"
            setattr(test_case, test_name, run_group)
        return test_case
    return decorate
