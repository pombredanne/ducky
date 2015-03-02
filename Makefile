SHELL := /bin/bash

CC_RED=$(shell echo -e "\033[0;31m")
CC_GREEN=$(shell echo -e "\033[0;32m")
CC_YELLOW=$(shell echo -e "\033[0;33m")
CC_END=$(shell echo -e "\033[0m")

SOURCES  := $(shell find $(CURDIR) -name '*.asm')
BINARIES := $(SOURCES:%.asm=%.bin)

FORTH_KERNEL := forth/ducky-forth.bin

forth/ducky-forth.bin: forth/ducky-forth.asm forth/ducky-forth-words.asm

.PHONY: tests-pre tests-engine tests-post test-submit-results tests docs cloc flake


#
# Tests
#
FORTH_TESTS_IN  := $(shell find $(CURDIR) -name 'test-*.f' | sort)
FORTH_TESTS_OUT := $(FORTH_TESTS_IN:%.f=%.f.out)

ifndef TESTSET
  TESTSET := default
endif

TESTSETDIR := $(CURDIR)/tests-$(TESTSET)

ifdef VMDEBUG
  VMDEBUG := -d
else
  VMDEBUG :=
endif

ifdef VMPROFILE
  VMPROFILE := -p $(TESTSETDIR)/profile
else
  VMPROFILE :=
endif

ifndef VMCOVERAGE
  VMCOVERAGE=yes
endif

ifdef BINPROFILE
  BINPROFILE := --machine-profile
else
  BINPROFILE :=
endif

ifndef CONIO_ECHO
  CONIO_ECHO := no
endif

ifndef CONIO_HIGHLIGHT
  CONIO_HIGHLIGHT := no
endif

ifndef CONIO_STDOUT_ECHO
  CONIO_STDOUT_ECHO := yes
endif

ifdef PYPY
	PYTHON := PYTHONPATH=$(VIRTUAL_ENV)/lib/python2.7/site-packages:$(CURDIR)/src:$(PYTHONPATH) /usr/bin/pypy
else
	PYTHON := PYTHONPATH=$(CURDIR)/src:$(PYTHONPATH)
endif


run:
ifeq ($(VMCOVERAGE),yes)
	$(eval VMCOVERAGE_FILE := COVERAGE_FILE="$(CURDIR)/.coverage.run")
	$(eval VMCOVERAGE_BIN  := $(VIRTUAL_ENV)/bin/coverage run)
else
	$(eval VMCOVERAGE_FILE := )
	$(eval VMCOVERAGE_BIN  := )
endif
	$(Q) $(VMCOVERAGE_FILE) PYTHONUNBUFFERED=yes $(PYTHON) $(VMCOVERAGE_BIN) tools/vm $(VMPROFILE) $(BINPROFILE) --conio-echo=no --conio-highlight=no --machine-config=tests/forth/test-machine.conf --machine-in=forth/ducky-forth.f

tests-pre:
	$(Q) echo -n "[TEST] Create test set $(TESTSET) ... "
	$(Q) rm -rf $(TESTSETDIR)
	$(Q) mkdir -p $(TESTSETDIR)
	$(Q) mkdir -p $(TESTSETDIR)/coverage
	$(Q) mkdir -p $(TESTSETDIR)/profile
	$(Q) mkdir -p $(TESTSETDIR)/results
	$(Q) $(CURDIR)/tests/xunit-record --init --file=$(TESTSETDIR)/results/forth.xml --testsuite=forth-$(TESTSET)
	$(Q) echo "$(CC_GREEN)PASS$(CC_END)"

tests-engine: tests/instructions/interrupts-basic.bin
	$(Q)  echo "[TEST] Engine unit tests"
ifeq ($(VMCOVERAGE),yes)
	$(eval VMCOVERAGE_FILE := COVERAGE_FILE="$(TESTSETDIR)/coverage/.coverage.engine")
	$(eval COVERAGE_NOSE_FLAG := --with-coverage)
else
	$(eval VMCOVERAGE_FILE := )
	$(eval COVERAGE_NOSE_FLAG := )
endif
	-$(Q) $(VMCOVERAGE_FILE) $(PYTHON) $(VIRTUAL_ENV)/bin/nosetests -v --all-modules $(COVERAGE_NOSE_FLAG) --with-xunit --xunit-file=$(TESTSETDIR)/results/nosetests.xml 2>&1 | stdbuf -oL -eL tee $(TESTSETDIR)/engine.out | grep -v -e '\[INFO\] ' -e '#> '
	-$(Q) sed -i 's/<testsuite name="nosetests"/<testsuite name="nosetests-$(TESTSET)"/' $(TESTSETDIR)/results/nosetests.xml

tests-forth-units: interrupts.bin $(FORTH_KERNEL) $(FORTH_TESTS_OUT)

tests-forth-ans: interrupts.bin $(FORTH_KERNEL)
	$(Q) echo -n "[TEST] FORTH ANS testsuite ... "
	$(eval tc_out      := $(TESTSETDIR)/forth-ans.out)
	$(eval tc_machine  := $(TESTSETDIR)/forth-ans.machine)
	$(eval tc_filtered := $(TESTSETDIR)/forth-ans.filtered)
ifeq ($(VMCOVERAGE),yes)
	$(eval VMCOVERAGE_FILE := COVERAGE_FILE="$(TESTSETDIR)/coverage/.coverage.forth-ans")
	$(eval VMCOVERAGE_BIN  := $(VIRTUAL_ENV)/bin/coverage run)
else
	$(eval VMCOVERAGE_FILE := )
	$(eval VMCOVERAGE_BIN  := )
endif
	-$(Q) $(VMCOVERAGE_FILE) $(PYTHON) $(VMCOVERAGE_BIN) tools/vm $(VMPROFILE) $(BINPROFILE) --machine-config=$(CURDIR)/tests/forth/test-machine.conf --machine-in=tests/forth/enable-test-mode.f --machine-in=forth/ducky-forth.f --machine-in=tests/forth/ans/tester.fr --machine-in=tests/forth/ans/core.fr --machine-out=$(tc_out) -g --conio-echo=$(CONIO_ECHO) --conio-console=no --conio-highlight=$(CONIO_HIGHLIGHT) --conio-stdout-echo=yes $(VMDEBUG) 2>&1 | stdbuf -oL -eL tee $(tc_machine) | grep -v -e '\[INFO\] ' -e '#> '
	-$(Q) grep -e 'INCORRECT RESULT' -e 'WRONG NUMBER OF RESULTS' $(tc_out) | cat > $(tc_filtered);
	-$(Q) if [ ! -s $(tc_filtered) ]; then \
				  $(CURDIR)/tests/xunit-record --add --file=$(TESTSETDIR)/results/forth.xml --ts=forth-$(TESTSET) --name="ANS test suite"; \
					echo "$(CC_GREEN)PASS$(CC_END)"; \
				else \
				  $(CURDIR)/tests/xunit-record --add --file=$(TESTSETDIR)/results/forth.xml --ts=forth-$(TESTSET) --name="ANS test suite" --result=fail --message="Failed aserts" --diff=$(tc_filtered); \
					echo "$(CC_RED)FAIL$(CC_END)"; \
					sed -e 's/^/  /' $(tc_filtered); \
				fi

tests-post:
	$(Q) cd $(TESTSETDIR)/coverage && coverage combine && cd ..
ifeq ($(VMCOVERAGE),yes)
	$(Q) COVERAGE_FILE="$(TESTSETDIR)/coverage/.coverage" coverage html --omit="*/python2.7/*" -d $(TESTSETDIR)/coverage/
endif

tests-submit-results:
ifdef CIRCLE_TEST_REPORTS
	$(eval ts_results := $(wildard $(TESTSETDIR)/results/*.xml))
	$(Q) mkdir -p $(CIRCLE_TEST_REPORTS)/$(TESTSET)
	$(Q) for f in `ls -1 $(TESTSETDIR)/results/*.xml`; do g="`basename $$f`"; cp $$f $(CIRCLE_TEST_REPORTS)/`echo "$$g" | sed 's/\(.*\).xml/$(TESTSET)-\1.xml/'`; done;
endif
ifdef CIRCLE_ARTIFACTS
	$(Q) cp -r $(TESTSETDIR) $(CIRCLE_ARTIFACTS)
endif

tests: tests-pre tests-engine tests-forth-units tests-forth-ans tests-post tests-submit-results

tests-engine-only: tests-pre tests-engine tests-post tests-submit-results

tests-forth-only: tests-pre tests-forth-units tests-post tests-submit-results


#
# Some utility targets
#

profile-eval:
	$(Q) python -i -c "import os; import pstats; ps = pstats.Stats(*['$(TESTSETDIR)/profile/%s' % f for f in os.listdir('$(TESTSETDIR)/profile/')])"

cloc:
	cloc --skip-uniqueness src/ forth/ examples/

flake:
	$(Q) flake8 --config=$(CURDIR)/flake8.cfg $(shell find $(CURDIR)/src $(CURDIR)/tests -name '*.py') $(shell find $(CURDIR)/tools) | sort | grep -v -e "'patch' imported but unused" -e tools/cc

docs:
	sphinx-apidoc -o docs/ src/
	make -C docs clean
	make -C docs html

clean:
	$(Q) rm -f $(BINARIES)


#
# Wildcard targets
#
%.bin: %.asm
	$(Q) echo -n "[COMPILE] $< => $@ ... "
	$(Q) $(PYTHON) tools/as -i $< -o $@ -f $(VMDEBUG); if [ "$$?" -eq 0 ]; then echo "$(CC_GREEN)PASS$(CC_END)"; else echo "$(CC_RED)FAIL$(CC_END)"; fi

%.f.out: %.f interrupts.bin $(FORTH_KERNEL)
	$(eval tc_name     := $(notdir $(<:%.f=%)))
	$(eval tc_coverage := $(TESTSETDIR)/coverage/.coverage.forth-unit.$(tc_name))
	$(eval tc_machine  := $(<:%.f=%.f.machine))
	$(eval tc_filtered := $(<:%.f=%.f.filtered))
	$(eval tc_expected := $(<:%.f=%.f.expected))
	$(eval tc_diff     := $(<:%.f=%.f.diff))
	$(eval tc_tmpfile  := $(shell mktemp))
ifeq ($(VMCOVERAGE),yes)
	$(eval VMCOVERAGE_FILE := COVERAGE_FILE="$(tc_coverage)")
	$(eval VMCOVERAGE_BIN  := $(VIRTUAL_ENV)/bin/coverage run)
else
	$(eval VMCOVERAGE_FILE := )
	$(eval VMCOVERAGE_BIN  := )
endif
	$(Q)  echo -n "[TEST] FORTH $(tc_name) ... "
	-$(Q) $(VMCOVERAGE_FILE) PYTHONUNBUFFERED=yes $(PYTHON) $(VMCOVERAGE_BIN) tools/vm $(VMPROFILE) $(BINPROFILE) -g --conio-stdout-echo=$(CONIO_STDOUT_ECHO) --conio-echo=$(CONIO_ECHO) --conio-highlight=$(CONIO_HIGHLIGHT) --conio-console=no --machine-config=tests/forth/test-machine.conf --machine-in=tests/forth/enable-test-mode.f --machine-in=forth/ducky-forth.f --machine-in=tests/forth/ans/tester.fr --machine-in=$< --machine-in=tests/forth/run-test-word.f --machine-out=$@ $(VMDEBUG) 2>&1 | stdbuf -oL -eL tee $(tc_machine) | grep -v -e '\[INFO\] ' -e '#> ' | cat
	-$(Q) grep -e 'INCORRECT RESULT' -e 'WRONG NUMBER OF RESULTS' $@ | cat > $(tc_filtered)
	-$(Q) if [ -f $(tc_expected) ]; then diff -u $(tc_expected) $@ | cat &> $(tc_diff); fi
	-$(Q) if [ ! -s $(tc_filtered) ] && ([ ! -f $(tc_diff) ] || [ ! -s $(tc_diff) ]); then \
				  $(CURDIR)/tests/xunit-record --add --file=$(TESTSETDIR)/results/forth.xml --ts=forth-$(TESTSET) --name=$(tc_name) --classname=$<; \
					echo "$(CC_GREEN)PASS$(CC_END)"; \
				else \
				  [ -f $(tc_filtered) ] && cat $(tc_filtered) >> $(tc_tmpfile); \
					[ -f $(tc_diff) ] && cat $(tc_diff) >> $(tc_tmpfile); \
				  $(CURDIR)/tests/xunit-record --add --file=$(TESTSETDIR)/results/forth.xml --ts=forth-$(TESTSET) --name=$(tc_name) --classname=$< --result=fail --message="Failed aserts" --diff=$(tc_tmpfile); \
					echo "$(CC_RED)FAIL$(CC_END)"; \
					sed 's/^/  /' $(tc_tmpfile); \
				fi; \
				rm -f $(tc_tmpfile)
	-$(Q) mv $@ $(TESTSETDIR)/
	-$(Q) if [ -f $(tc_machine) ]; then mv $(tc_machine) $(TESTSETDIR)/; fi
	-$(Q) if [ -f $(tc_diff) ]; then mv $(tc_diff) $(TESTSETDIR)/; fi
	-$(Q) if [ -f $(tc_filtered) ]; then mv $(tc_filtered) $(TESTSETDIR)/; fi
