/// Compares loading with one commit per row (batchSize=1) against batched commits.
///
/// Run it from the IRIS terminal:
/// <example>
/// do ##class(HF.Benchmark).Run("stanfordnlp/imdb", "train", 5000, 1000)
/// </example>
/// A warm-up load runs first so the dataset download/cache does not skew the comparison.
Class HF.Benchmark Extends %RegisteredObject
{

ClassMethod Run(dataset As %String = "stanfordnlp/imdb", split As %String = "train", limit As %Integer = 5000, batchSize As %Integer = 1000) As %Status
{
	Write "Warm-up (not timed)...", !
	Do ..Drop("Bench.Warmup")
	Set json = ##class(HF.Loader).Load(dataset, split, "Bench.Warmup", limit)
	Do ..Drop("Bench.Warmup")
	If ({}.%FromJSON(json).status '= "ok") {
		Write "Warm-up failed: ", json, !
		Quit $$$ERROR($$$GeneralError, json)
	}

	Do ..Time(dataset, split, "Bench.RowByRow", limit, 1)
	Do ..Time(dataset, split, "Bench.Batched", limit, batchSize)
	Quit $$$OK
}

ClassMethod Time(dataset As %String, split As %String, target As %String, limit As %Integer, batchSize As %Integer)
{
	Do ..Drop(target)
	Set start = $ZHOROLOG
	Set json = ##class(HF.Loader).Load(dataset, split, target, limit, "", batchSize)
	Set elapsed = $ZHOROLOG - start
	Set status = {}.%FromJSON(json).status
	Write target, "  batchSize=", batchSize, "  status=", status, "  elapsed=", $FNUMBER(elapsed, "", 2), "s", !
	If status '= "ok" Write "  ", json, !
	Do ..Drop(target)
}

ClassMethod Drop(table As %String) [ Private ]
{
	Do ##class(%SQL.Statement).%ExecDirect(, "DROP TABLE " _ table)
}

}
