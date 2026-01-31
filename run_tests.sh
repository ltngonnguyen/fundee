#!/bin/bash
echo "Running Unit Tests..."
python3 -m coverage run -m pytest test_units.py

echo "Generating Report..."
python3 -m coverage report
