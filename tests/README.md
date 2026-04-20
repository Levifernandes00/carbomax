# Carbomax Tests

Run tests from the repository root:

```bash
python3 -m pytest carbomax/tests
```

Run only parser tests:

```bash
python3 -m pytest carbomax/tests -k "extract_numbers or parse_curve"
```

Run only polling tests:

```bash
python3 -m pytest carbomax/tests -k "polling"
```
