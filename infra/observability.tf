# Observability.
#
# The claim this exists to support is "it has been running for months, this is
# the p99, this is what broke and this is how I found out". Deploying something
# and operating it are different sentences, and the second one needs numbers.
#
# Everything here is inside the CloudWatch free tier at this traffic: ten custom
# metrics, ten alarms and three dashboards are free, and the metrics arrive as
# EMF inside log lines the Lambda was already writing.

# Where alarms go. An alarm that only changes a colour on a console nobody has
# open is not an alarm.
resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email

  # AWS emails a confirmation link and the subscription stays "pending" until
  # it is clicked. Terraform reports success either way, so an unconfirmed
  # subscription is a silent no-op — check the topic after the first apply.
}

# ---- the alarm that closes the loop ----
#
# agent/graph.py falls back to another model when the pinned one is retired, so
# the demo survives. This is the half that tells somebody it happened. Without
# it the fallback is just a slower way of not knowing: the endpoint answers, the
# published evaluation numbers quietly stop describing what is running, and the
# first person to notice is whoever compares them.
resource "aws_cloudwatch_metric_alarm" "model_substituted" {
  alarm_name          = "${var.name}-model-substituted"
  alarm_description   = <<-EOT
    The pinned Groq model is no longer in the catalogue and the agent fell back
    to another one. Nothing is down. What is wrong is that the README's
    evaluation numbers were measured on the retired model and no longer describe
    the running system: re-pin a model and re-run the evals.
  EOT
  namespace           = "BusinessOpsMCP"
  metric_name         = "ModelSubstituted"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  # Missing data is the normal state here: the metric only exists on the day a
  # model is retired. Treating absence as breaching would page forever.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
}

# ---- the ordinary two ----

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.name}-errors"
  alarm_description   = "The function is throwing. Check the log group."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.this.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "slow" {
  alarm_name        = "${var.name}-p99-latency"
  alarm_description = <<-EOT
    p99 request latency above ${var.p99_threshold_ms}ms across two consecutive
    five-minute windows. Two windows rather than one because a single cold start
    can carry a window on this traffic, and an alarm that cries wolf gets muted,
    which is worse than not having it.
  EOT
  namespace         = "BusinessOpsMCP"
  metric_name       = "RequestLatencyMs"
  # p99 of the warm path. Cold starts are several seconds by nature and are
  # counted separately, so mixing them in would make this a statement about
  # container churn rather than about the code.
  extended_statistic  = "p99"
  dimensions          = { Kind = "mcp", Outcome = "ok" }
  period              = 300
  evaluation_periods  = 2
  threshold           = var.p99_threshold_ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# ---- the dashboard ----
#
# Its job is to answer "how has this behaved for the last month" in one screen,
# which is the question an interview actually asks.
resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = var.name

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6,
        properties = {
          title  = "Request latency (warm path)"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["BusinessOpsMCP", "RequestLatencyMs", "Kind", "mcp", "Outcome", "ok",
            { stat = "p50", label = "p50" }],
            ["...", { stat = "p99", label = "p99" }],
          ]
          yAxis = { left = { label = "ms", showUnits = false } }
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6,
        properties = {
          title   = "Requests by outcome"
          region  = var.region
          view    = "timeSeries"
          stacked = true
          metrics = [
            ["BusinessOpsMCP", "Requests", "Kind", "mcp", "Outcome", "ok", { stat = "Sum" }],
            ["BusinessOpsMCP", "Requests", "Kind", "mcp", "Outcome", "error", { stat = "Sum" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6,
        properties = {
          title  = "Cold starts"
          region = var.region
          view   = "timeSeries"
          metrics = [["BusinessOpsMCP", "ColdStart", "Kind", "mcp", "Outcome", "ok",
          { stat = "Sum" }]]
        }
      },
      {
        type = "log", x = 12, y = 6, width = 12, height = 6,
        properties = {
          title  = "Recent errors"
          region = var.region
          query  = <<-EOT
            SOURCE '${aws_cloudwatch_log_group.lambda.name}'
            | fields @timestamp, @message
            | filter @message like /ERROR|Traceback|WARNING/
            | sort @timestamp desc
            | limit 20
          EOT
        }
      },
    ]
  })
}
