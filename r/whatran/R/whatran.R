# whatran for R: a thin shell around the whatran command line.
# Every call becomes one `whatran ...` invocation, so the ledger, the writing rules
# (verb-first labels, short plain sentences) and the graph are shared with Python.

#' Locate the whatran command.
#'
#' Order: the WHATRAN_CLI environment variable (e.g. "python3 /path/whatran.py"),
#' then `whatran` on the PATH, then analysis/whatran.py in the project found by
#' walking up from the working directory.
whatran_cli <- function() {
  env <- Sys.getenv("WHATRAN_CLI", unset = "")
  if (nzchar(env)) return(strsplit(env, "[[:space:]]+")[[1]])
  on_path <- Sys.which("whatran")
  if (nzchar(on_path)) return(unname(on_path))
  dir <- normalizePath(getwd())
  repeat {
    cand <- file.path(dir, "analysis", "whatran.py")
    if (file.exists(cand)) return(c("python3", cand))
    parent <- dirname(dir)
    if (identical(parent, dir)) break
    dir <- parent
  }
  stop("whatran not found: install it (pip install whatran), set WHATRAN_CLI, or run `whatran init` in the project")
}

.wt_call <- function(args) {
  cli <- whatran_cli()
  out <- suppressWarnings(system2(cli[1], c(cli[-1], shQuote(args)), stdout = TRUE, stderr = TRUE))
  status <- attr(out, "status")
  if (!is.null(status) && status != 0) stop(paste(out, collapse = "\n"), call. = FALSE)
  out
}

.wt_id <- function(out) {
  line <- grep("^recorded ", out, value = TRUE)
  if (!length(line)) stop(paste(out, collapse = "\n"), call. = FALSE)
  sub("^recorded ([^[:space:]]+).*", "\\1", line[1])
}

.wt_flags <- function(flag, values) {
  if (is.null(values) || !length(values)) return(character())
  as.vector(rbind(flag, as.character(values)))
}

.wt_params <- function(params) {
  if (is.null(params) || !length(params)) return(character())
  vals <- vapply(names(params), function(k) {
    v <- params[[k]]
    v <- if (length(v) > 1) paste(v, collapse = ",") else as.character(v)
    paste0(k, "=", v)
  }, character(1))
  as.vector(rbind("--param", unname(vals)))
}

.wt_opt <- function(flag, value) if (is.null(value)) character() else c(flag, value)

#' Record a step that already ran (its command was captured by hooks or whatran_run).
#' Returns the run id.
whatran_record <- function(label, inputs = NULL, outputs = NULL, code = NULL, params = list(),
                           evidence = "auto", note = NULL, supersedes = NULL, command = NULL) {
  .wt_id(.wt_call(c("record", label, .wt_flags("--in", inputs), .wt_flags("--out", outputs),
                    .wt_flags("--code", code), .wt_params(params), "--evidence", evidence,
                    .wt_opt("--note", note), .wt_opt("--supersedes", supersedes), .wt_opt("--cmd", command))))
}

#' Evaluate `expr` as a recorded step. Files that change while it runs become the
#' step's outputs. Returns the run id; errors inside `expr` are re-raised after the
#' step is recorded as failed.
whatran_step <- function(label, expr, params = list(), inputs = NULL, outputs = NULL, code = NULL,
                         note = NULL, supersedes = NULL) {
  token <- trimws(.wt_call(c("begin", label, .wt_flags("--code", code), .wt_opt("--note", note)))[1])
  status <- "completed"
  result <- tryCatch(expr, error = function(e) { status <<- "failed"; e })
  out <- .wt_call(c("end", token, .wt_flags("--in", inputs), .wt_flags("--out", outputs),
                    .wt_params(params), "--status", status, .wt_opt("--supersedes", supersedes)))
  if (inherits(result, "error")) stop(result)
  .wt_id(out)
}

#' Record a finding and what it rests on (run ids or output paths). Returns the claim id.
whatran_claim <- function(text, supports = NULL, note = NULL) {
  .wt_id(.wt_call(c("claim", text, .wt_flags("--supports", supports), .wt_opt("--note", note))))
}

#' Abandon a run or claim with a reason; it stays in the record.
whatran_reject <- function(id, reason) {
  invisible(.wt_call(c("reject", id, "--reason", reason)))
}

#' Amend a record's label, note or parameters.
whatran_edit <- function(id, label = NULL, note = NULL, params = list()) {
  invisible(.wt_call(c("update", id, .wt_opt("--label", label), .wt_opt("--note", note), .wt_params(params))))
}

#' Retract a record from the graph; the ledger keeps it.
whatran_remove <- function(id, reason = NULL) {
  invisible(.wt_call(c("remove", id, .wt_opt("--reason", reason))))
}

#' Run a shell command with captured evidence, optionally recording it as a step.
whatran_run <- function(command, label = NULL, inputs = NULL, outputs = NULL, code = NULL, params = list(),
                        note = NULL) {
  out <- .wt_call(c("run", .wt_opt("--label", label), .wt_flags("--in", inputs), .wt_flags("--out", outputs),
                    .wt_flags("--code", code), .wt_params(params), .wt_opt("--note", note), "--", command))
  invisible(out)
}
