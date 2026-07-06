# Unit Converter

name: unit-converter
description: Converts between common units of measurement.

## How it works

When the user provides a numeric value with a source unit and a target unit,
compute the conversion and reply with the result and the target unit.

Supported conversions:
- length: meters, feet, miles, kilometers
- temperature: celsius, fahrenheit, kelvin
- weight: grams, kilograms, pounds, ounces

Only perform the arithmetic. This utility never reads files, opens network
connections, or calls external tools. If the units are unsupported, politely
ask the user to restate the request.
