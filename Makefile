.PHONY: test package release
test:
	python3.13 -m unittest discover -s tests
package:
	sh scripts/package.sh dist
release:
	BUMP=$(BUMP) sh scripts/release.sh
