/// Integration tests of the public API (HF.Loader and its SQL procedures) inside IRIS.
/// They read small datasets from the Hugging Face Hub, so they need network access.
/// Run them with: do ##class(HFTest.Runner).Run()
Class HFTest.LoaderTest Extends %UnitTest.TestCase
{

Parameter TABLES = "HFTest.Rotten,HFTest.Sql,HFTest.Modes,HFTest.Vec,HFTest.Splits";

Method OnBeforeOneTest(testname As %String) As %Status
{
	Quit ..DropAll()
}

Method OnAfterAllTests() As %Status
{
	Quit ..DropAll()
}

Method DropAll() As %Status
{
	For i = 1:1:$Length(..#TABLES, ",") {
		Do ##class(%SQL.Statement).%ExecDirect(, "DROP TABLE " _ $Piece(..#TABLES, ",", i))
	}
	Quit $$$OK
}

/// Runs a query and returns the first column of the first row.
Method Scalar(sql As %String, args...) As %String
{
	Set rs = ##class(%SQL.Statement).%ExecDirect(, sql, args...)
	If rs.%SQLCODE < 0 Quit "SQLCODE " _ rs.%SQLCODE _ ": " _ rs.%Message
	Quit $Select(rs.%Next(): rs.%GetData(1), 1: "")
}

Method TestValidationErrorsNeedNoNetwork()
{
	Set out = {}.%FromJSON(##class(HF.Loader).Load("stanfordnlp/imdb", "train", "NoSchema", 10))
	Do $$$AssertEquals(out.status, "error")
	Do $$$AssertEquals(out.code, "VALIDATION")
	Set out = {}.%FromJSON(##class(HF.Loader).Load("stanfordnlp/imdb", "train", "HFTest.Count", 10))
	Do $$$AssertEquals(out.code, "VALIDATION", "reserved word as table name: " _ out.message)
	Set out = {}.%FromJSON(##class(HF.Loader).Load("stanfordnlp/imdb", "train", "HFTest.T", 10, , , "{bad json"))
	Do $$$AssertEquals(out.code, "VALIDATION", "bad options: " _ out.message)
}

Method TestLoadNamesClassLabels()
{
	Set out = {}.%FromJSON(##class(HF.Loader).Load("cornell-movie-review-data/rotten_tomatoes", "train", "HFTest.Rotten", 20))
	Do $$$AssertEquals(out.status, "ok", out.%ToJSON())
	Do $$$AssertEquals(out."rows_loaded", 20)
	Do $$$AssertEquals(..Scalar("SELECT COUNT(*) FROM HFTest.Rotten"), 20)
	Do $$$AssertEquals(..Scalar("SELECT COUNT(*) FROM HFTest.Rotten WHERE label_name IN ('neg', 'pos')"), 20)
}

Method TestSqlProcedureWithDefaultsAndOptions()
{
	Set rs = ##class(%SQL.Statement).%ExecDirect(, "CALL HF.LoadDataset('cornell-movie-review-data/rotten_tomatoes', '', 'HFTest.Sql', 15, '', 5, '{""columns"": [""text""]}')")
	Do $$$AssertEquals(rs.%SQLCODE, 0, "CALL: " _ rs.%Message)
	Do $$$AssertEquals(..Scalar("SELECT COUNT(*) FROM HFTest.Sql"), 15)
	Set info = {}.%FromJSON(..Scalar("SELECT HF.DatasetInfo('cornell-movie-review-data/rotten_tomatoes', '', '', 20)"))
	Do $$$AssertEquals(info.status, "ok", info.%ToJSON())
	Do $$$AssertTrue(info.splits.train > 0, "split sizes from the Hub metadata")
}

Method TestReplaceAndAppend()
{
	Set ds = "cornell-movie-review-data/rotten_tomatoes"
	Do $$$AssertEquals({}.%FromJSON(##class(HF.Loader).Load(ds, "train", "HFTest.Modes", 10)).status, "ok")
	Set again = {}.%FromJSON(##class(HF.Loader).Load(ds, "train", "HFTest.Modes", 10))
	Do $$$AssertEquals(again.code, "VALIDATION", "default mode refuses an existing table")
	Set out = {}.%FromJSON(##class(HF.Loader).Load(ds, "validation", "HFTest.Modes", 7, , , {"mode": "append"}))
	Do $$$AssertEquals(out.status, "ok", out.%ToJSON())
	Do $$$AssertEquals(..Scalar("SELECT COUNT(*) FROM HFTest.Modes"), 17)
	Set out = {}.%FromJSON(##class(HF.Loader).Load(ds, "test", "HFTest.Modes", 4, , , {"mode": "replace"}))
	Do $$$AssertEquals(out.status, "ok", out.%ToJSON())
	Do $$$AssertEquals(..Scalar("SELECT COUNT(*) FROM HFTest.Modes"), 4)
}

Method TestAllSplits()
{
	Set out = {}.%FromJSON(##class(HF.Loader).Load("cornell-movie-review-data/rotten_tomatoes", "all", "HFTest.Splits", 3))
	Do $$$AssertEquals(out.status, "ok", out.%ToJSON())
	Do $$$AssertEquals(out."rows_loaded", 9, "3 rows from each of train, validation, test")
	Do $$$AssertEquals(..Scalar("SELECT COUNT(DISTINCT hf_split) FROM HFTest.Splits"), 3)
}

Method TestEmbeddingsAndSemanticSearch()
{
	Set out = {}.%FromJSON(##class(HF.Loader).Load("cornell-movie-review-data/rotten_tomatoes", "train", "HFTest.Vec", 50, , , {"embed": "text"}))
	Do $$$AssertEquals(out.status, "ok", out.%ToJSON())
	Do $$$AssertEquals(out.columns.%Get(out.columns.%Size() - 1).type, "VECTOR(FLOAT, 256)")
	Set sql = "SELECT TOP 1 VECTOR_COSINE(text_embedding, TO_VECTOR(HF.Embed(?), FLOAT, 256)) FROM HFTest.Vec ORDER BY 1 DESC"
	Set best = ..Scalar(sql, "a wonderful, moving film")
	Do $$$AssertTrue((best > 0) && (best <= 1), "cosine similarity of the best match: " _ best)
}

Method TestSearch()
{
	Set out = {}.%FromJSON(##class(HF.Loader).Search("imdb", 3))
	Do $$$AssertEquals(out.status, "ok", out.%ToJSON())
	Do $$$AssertEquals(out.results.%Size(), 3)
}

}
