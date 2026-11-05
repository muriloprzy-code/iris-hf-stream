/// Runs the HFTest unit tests and prints one line per test plus a final verdict
/// ("ALL TESTS PASSED" or "TESTS FAILED"), which the CI workflow checks.
/// <example>do ##class(HFTest.Runner).Run()</example>
Class HFTest.Runner
{

/// Test classes, run in this order.
Parameter CASES = "HFTest.LoaderTest,HFTest.RESTTest";

/// Folder that holds this suite (tests-iris/HFTest) inside the image.
Parameter ROOT = "/opt/irisapp/tests-iris/HFTest/";

ClassMethod Run() As %Status
{
	// The classes are already compiled into the image: run them without loading files.
	// %UnitTest still wants a valid root directory for the suite.
	Set ^UnitTestRoot = ##class(%File).NormalizeDirectory(##class(%File).ParentDirectoryName(..#ROOT))
	Set sc = $$$OK, failed = 0, total = 0
	Write !
	For c = 1:1:$Length(..#CASES, ",") {
		Set sc1 = ##class(%UnitTest.Manager).DebugRunTestCase("HFTest", $Piece(..#CASES, ",", c), "/noload/nodelete")
		Set:$$$ISERR(sc1) sc = sc1
		Write !
		// Each run writes a new ^UnitTest.Result index: report the latest one.
		Set index = $Order(^UnitTest.Result(""), -1)
		Continue:index=""
		Set suite = ""
		For {
			Set suite = $Order(^UnitTest.Result(index, suite)) Quit:suite=""
			Set case = ""
			For {
				Set case = $Order(^UnitTest.Result(index, suite, case)) Quit:case=""
				Set method = ""
				For {
					Set method = $Order(^UnitTest.Result(index, suite, case, method)) Quit:method=""
					Set status = $ListGet(^UnitTest.Result(index, suite, case, method), 1)
					Set total = total + 1
					If status '= 1 Set failed = failed + 1
					Write $Select(status = 1: "PASS  ", 1: "FAIL  "), case, ":", method, !
				}
			}
		}
	}
	If (total > 0) && (failed = 0) && $$$ISOK(sc) {
		Write total, " tests, ALL TESTS PASSED", !
		Quit $$$OK
	}
	Write total, " tests, ", failed, " failed: TESTS FAILED", !
	Quit $$$ERROR($$$GeneralError, "unit tests failed")
}

}
