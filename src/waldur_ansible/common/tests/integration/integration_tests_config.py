import sys

TEST_TAG_FLAG = "--tag"
INTEGRATION_TEST = "integration"
INTEGRATION_FLAG = "%s=%s" % (TEST_TAG_FLAG, INTEGRATION_TEST)
SKIP_INTEGRATION_REASON = 'To run integration tests provide %s flag' % INTEGRATION_FLAG


def integration_test_flag_provided():
    return INTEGRATION_FLAG in sys.argv
