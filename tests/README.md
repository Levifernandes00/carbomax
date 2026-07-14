# Carbomax Tests

Run tests from the carbomax directory:

```bash
cd carbomax
python3 -m pytest tests
```

Or from the integrations parent repo (with submodules checked out):

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
