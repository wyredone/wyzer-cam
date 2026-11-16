.PHONY: prepare test

# Check out thingino, copy aiwatch in, apply the patches, enable the v3 configs.
prepare:
	sh scripts/prepare-thingino.sh

test:
	sh test/run-tests.sh
	python -m unittest discover -s test
