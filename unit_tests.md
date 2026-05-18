# Unit Test

## Ambitions

* Unit tests should verify actions that the user may do.
* Tests should be focused. minimal and fast
* We will only do basic testing of erroneous Rest API commands that are not possible through the UI. This should primarily cover security testing, to avoid REST api attacks from a malicious actor that gets access to the API but does not otherwise have access to the local disk.
* All tests are run for each release
* Tests that are marked @pytest.mark.release are only run each release - not for every commit. These are tests that are less likely to fail.
* We prefer a fast test that is covering all the required code paths at a high level over low level tests.



## Not-ambitions

Tests should not be:

* More verbose than necessary
* Redundant or strictly not necessary to test. (Example 1: We don't need to test that git and os functions behave as specified, we only test that our applications' use of them are correct. Example 2: We don't need to test issues that will never occur)
* Duplicated or covered by tests in several abstraction levels. (e.g. it is sufficient that a single test fails when we have a regression. We don't need to have extra unit tests to specify *what* regressed as long as we have coverage that ensures that some unit test fails when we have a regression)
* Dead code
* Duplicated test fixtures,  setup, data or test repo.

