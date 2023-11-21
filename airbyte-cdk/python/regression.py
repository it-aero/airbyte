#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#
import asyncio
import dataclasses
import json
import subprocess

import matplotlib.pyplot as plt
import pandas as pd
import yaml
from aiostream import stream
from airbyte_cdk.models import (
    AirbyteMessage,
    AirbyteRecordMessage,
    AirbyteStream,
    ConfiguredAirbyteCatalog,
    ConfiguredAirbyteStream,
    DestinationSyncMode,
    SyncMode,
)
from airbyte_cdk.models import Type as MessageType
from matplotlib.backends.backend_pdf import PdfPages
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


@dataclasses.dataclass
class Message:
    prefix: str
    message: AirbyteMessage


@dataclasses.dataclass
class StreamStats:
    stream: str
    record_count = 0

    columns_to_diff_count = {}
    columns_to_right_missing = {}
    columns_to_left_missing = {}
    columns_to_equal = {}

    left_rows_missing = {}
    right_rows_missing = {}
    mismatch = []
    fields_to_ignore = set()


async def main():
    connector = "source-stripe"
    connector_version = "4.5.4"
    config_path = "secrets/prod_config_recent_only.json"
    regression_config_path = "regression_config.yaml"
    with open(regression_config_path) as f:
        regression_config = yaml.safe_load(f)

    stream_configs = regression_config["streams"]
    stream_to_stream_config = {stream_config["name"]: stream_config for stream_config in stream_configs}
    stream_names = [stream_config["name"] for stream_config in stream_configs]
    discover_command = f"docker run --rm -v $(pwd)/secrets:/secrets -v $(pwd)/integration_tests:/integration_tests airbyte/{connector}:{connector_version} discover --config /{config_path}"
    discover_result = subprocess.run(discover_command, shell=True, check=True, stdout=subprocess.PIPE, text=True)
    discover_output = discover_result.stdout
    catalog = None
    for discover_line in discover_output.split("\n"):
        if "CATALOG" in discover_line:
            print(discover_line)
            discover_message = AirbyteMessage.parse_raw(discover_line)
            catalog = discover_message.catalog
            break
    if not catalog:
        print(f"Could not find catalog in {discover_output}")
    streams = [stream for stream in catalog.streams if stream.name in stream_names]
    with open(f"secrets/tmp_catalog.json", "w") as f:
        f.write(_configured_catalog(streams).json(exclude_unset=True))
    command = f"docker run --rm -v $(pwd)/secrets:/secrets -v $(pwd)/integration_tests:/integration_tests airbyte/{connector}:{connector_version} read --config /{config_path} --catalog /secrets/tmp_catalog.json"

    subprocess_left = run_subprocess(command, "left")
    subprocess_right = run_subprocess(command, "right")

    streams_stats = {}
    primary_key = "id"
    # FIXME: need to get pk from catalog
    streams_to_dataframe = {}

    while subprocess_left.__anext__() and subprocess_right.__anext__():
        try:
            left = await subprocess_left.__anext__()
            right = await subprocess_right.__anext__()

            if left.message.type != right.message.type:
                print(f"Type mismatch: {left.message.type} != {right.message.type}")
                print(left)
                print()
                print(right)
                print(left.message.type == right.message.type)
                return
            if left.message.type == MessageType.RECORD:

                assert left.message.record.stream == right.message.record.stream
                if left.message.record.stream not in streams_stats:
                    streams_stats[left.message.record.stream] = StreamStats(left.message.record.stream)
                    streams_stats[left.message.record.stream].fields_to_ignore = set(
                        stream_to_stream_config[left.message.record.stream].get("ignore_fields", [])
                    )

                stream_stats = streams_stats[left.message.record.stream]

                stream_stats.record_count += 1
                if left.message.record.data[primary_key] != right.message.record.data[primary_key]:
                    stream_stats.left_rows_missing[left.message.record.data[primary_key]] = left
                    stream_stats.right_rows_missing[right.message.record.data[primary_key]] = right
                    continue

                compare_records(left, right, stream_stats)

                if left.message.record.data != right.message.record.data:
                    print(f"Data mismatch: {left.message.record.data} != {right.message.record.data}")
                    stream_stats.mismatch.append((left, right))

                # check missing
                left_keys = set(stream_stats.left_rows_missing.keys())
                for left_key in left_keys:
                    print(f"missinh {left_key}")
                    if left_key in stream_stats.right_rows_missing:
                        print(f"found {left_key} in right")
                        compare_records(stream_stats.left_rows_missing[left_key], stream_stats.right_rows_missing[left_key], stream_stats)
                        stream_stats.left_rows_missing.pop(left_key)
                        stream_stats.right_rows_missing.pop(left_key)
                    else:
                        print(f"did not find {left_key} in right")
        except StopAsyncIteration:
            # FIXME needd to do another check of missing
            # check missing
            for stream_name, stream_stats in streams_stats.items():
                left_keys = set(stream_stats.left_rows_missing.keys())
                for left_key in left_keys:
                    print(f"missinh {left_key}")
                    if left_key in stream_stats.right_rows_missing:
                        print(f"found {left_key} in right")
                        compare_records(stream_stats.left_rows_missing[left_key], stream_stats.right_rows_missing[left_key], stream_stats)
                        stream_stats.left_rows_missing.pop(left_key)
                        stream_stats.right_rows_missing.pop(left_key)
                    else:
                        print(f"did not find {left_key} in right")
            # FIXME need to check if both are done
            for stream_stats in streams_stats.values():
                stats_rows = []
                for column in stream_stats.columns_to_diff_count:
                    stats_rows.append(
                        {
                            "stream": stream_stats.stream,
                            "column": column,
                            "metric": "diff_count",
                            "value": stream_stats.columns_to_diff_count[column],
                        }
                    )
                for column in stream_stats.columns_to_equal:
                    stats_rows.append(
                        {
                            "stream": stream_stats.stream,
                            "column": column,
                            "metric": "equal_count",
                            "value": stream_stats.columns_to_equal[column],
                        }
                    )
                df = pd.DataFrame.from_records(stats_rows)
                streams_to_dataframe[stream_stats.stream] = df
                print(f"done processing {stream_stats.record_count} records")
                print(f"columns_to_diff_count: {stream_stats.columns_to_diff_count}")
                print(f"columns_to_right_missing: {stream_stats.columns_to_right_missing}")
                print(f"columns_to_left_missing: {stream_stats.columns_to_left_missing}")
                print(f"columns_to_equal: {stream_stats.columns_to_equal}")
                # NEED TO VERIFY BOTH ARE DONE

                print(f"left_rows_missing: {stream_stats.left_rows_missing}")
                print(len(stream_stats.left_rows_missing))

                print(f"right_rows_missing: {stream_stats.right_rows_missing}")
                print(len(stream_stats.right_rows_missing))

            break
    generate_plots_single_pdf_per_metric(streams_to_dataframe, streams_stats)


def _configured_catalog(streams):
    return ConfiguredAirbyteCatalog(
        streams=[
            ConfiguredAirbyteStream(
                stream=stream,
                sync_mode=SyncMode.full_refresh,
                destination_sync_mode=DestinationSyncMode.append,
            )
            for stream in streams
        ]
    )


def generate_plots_single_pdf_per_metric(streams_to_dataframe, streams_stats, output_filename="plots_combined_per_metric.pdf"):
    # diff_pdf_filename = "diff.pdf"
    # diff_pdf = canvas.Canvas(diff_pdf_filename)
    diff_filename = "diff_{stream}.jsonl"
    with PdfPages(output_filename) as pdf:

        # Generate summary
        # TODO
        summary_stats = []
        for stream_name, stream_stat in streams_stats.items():
            any_diff = any([val > 0 for val in stream_stat.columns_to_diff_count.values()])
            print(f"diff for stream {stream_name}: {stream_stat.columns_to_diff_count}")
            stat = {
                "stream": stream_name,
                "equal": not any_diff,
                "record_count": stream_stat.record_count,
                "missing_left": len(stream_stat.left_rows_missing),
                "missing_right": len(stream_stat.right_rows_missing),
            }
            print(f"stat:{stat}")
            summary_stats.append(stat)

            with open(diff_filename.format(stream=stream_name), "w") as diff_file:
                for left, right in stream_stat.mismatch:
                    left_data_json = json.dumps({"left": left.message.record.data, "right": right.message.record.data})
                    diff_file.write(left_data_json)

        # diff_pdf.save()
        # print(f"Diff saved to {diff_pdf_filename}")
        print(f"summary_stats:{summary_stats}")
        summary_df = pd.DataFrame.from_records(summary_stats)
        # table = pd.pivot_table(summary_df, index='stream', columns=['equal', "record_count", "missing_left", "missing_right"], aggfunc=len, fill_value=0)
        # table = pd.pivot_table(summary_df, index='stream', columns=["record_count"], aggfunc=len, fill_value=0)
        table = summary_df.pivot_table(values=["equal", "record_count", "missing_left", "missing_right"], index="stream", aggfunc="first")
        plt.figure(figsize=(6, 4))
        plt.table(cellText=table.values, colLabels=table.columns, rowLabels=table.index, loc="center")
        plt.title(f"Summary stats")
        plt.axis("off")  # Hide axis
        pdf.savefig(bbox_inches="tight", pad_inches=1)
        plt.close()

        for stream, group_data in streams_to_dataframe.items():
            # Generate per-stream tables
            table = pd.pivot_table(group_data, values="value", index="column", columns="metric")
            # Plotting table using matplotlib

            # Define a function to assign colors based on conditions
            def color_cells(table, row, col):
                value = table.loc[row, col]
                if value > 0 and col != "equal_count":
                    s = table.loc[row].name
                    if s in stream_stat.fields_to_ignore:
                        return "yellow"
                    else:
                        return "red"
                return "white"  # Default color for other cells

            # Convert table data to a list for cell colors
            cell_colors = [[color_cells(table, row, col) for col in table.columns] for row in table.index]

            plt.figure(figsize=(6, 4))
            plt.title(f"Stream: {stream}", y=3.2)
            plt.table(cellText=table.values, colLabels=table.columns, rowLabels=table.index, cellColours=cell_colors, loc="center")
            plt.axis("off")  # Hide axis

            # Save the table as a page in the PDF
            pdf.savefig(bbox_inches="tight", pad_inches=1)
            plt.close()

        for stream, group_data in streams_to_dataframe.items():
            grouped = group_data.groupby("column")
            for column, group_data in grouped:
                # Create a table for each stream and column combination
                table = pd.pivot_table(group_data, values="value", index="metric", columns="column")

                # Plotting table using matplotlib
                plt.figure(figsize=(6, 4))
                plt.table(cellText=table.values, colLabels=table.columns, rowLabels=table.index, loc="center")
                plt.title(f"Stream: {stream}, Column: {column}")
                plt.axis("off")  # Hide axis

                # Save the table as a page in the PDF
                pdf.savefig(bbox_inches="tight", pad_inches=1)
                plt.close()

        print(f"Tables saved to {output_filename}")


def compare_records(left, right, stream_stats):
    for column, left_value in left.message.record.data.items():
        if column not in stream_stats.columns_to_diff_count:
            stream_stats.columns_to_diff_count[column] = 0
        if column not in stream_stats.columns_to_right_missing:
            stream_stats.columns_to_right_missing[column] = 0
        if column not in stream_stats.columns_to_left_missing:
            stream_stats.columns_to_left_missing[column] = 0
        if column not in stream_stats.columns_to_equal:
            stream_stats.columns_to_equal[column] = 0

        if column not in right.message.record.data:
            stream_stats.columns_to_right_missing[column] += 1
            continue
        elif left_value != right.message.record.data[column]:
            stream_stats.columns_to_diff_count[column] += 1
        else:
            stream_stats.columns_to_equal[column] += 1
    for column, right_value in right.message.record.data.items():
        if column not in stream_stats.columns_to_diff_count:
            stream_stats.columns_to_diff_count[column] = 0
        if column not in stream_stats.columns_to_right_missing:
            stream_stats.columns_to_right_missing[column] = 0
        if column not in stream_stats.columns_to_left_missing:
            stream_stats.columns_to_left_missing[column] = 0
        if column not in stream_stats.columns_to_equal:
            stream_stats.columns_to_equal[column] = 0
        if column not in left.message.record.data:
            stream_stats.columns_to_left_missing[column] += 1


async def is_next_item_available(generator):
    async for _ in asyncio.as_completed([generator.__anext__()]):
        return True


async def run_subprocess(command, suffix):
    # Create a subprocess
    process = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    # Read lines from stdout asynchronously
    async def read_lines(stream):
        async for line in stream:
            yield line.decode().rstrip()

    # Start reading lines from both stdout and stderr
    stdout_lines = read_lines(process.stdout)
    stderr_lines = read_lines(process.stderr)

    # Consume lines from both streams concurrently
    async for line in stdout_lines:
        yield Message(prefix=f"{suffix}: ", message=AirbyteMessage.parse_raw(line))

    # # Wait for the process to finish
    # await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
