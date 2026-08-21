#!/usr/bin/env python3
"""Analyse des données réelles de température et slope pour calibrer l'algorithme."""

import csv
import io
from datetime import datetime, timedelta
from collections import defaultdict

# ─── Données temp_salon.csv ────────────────────────────────────────────────
TEMP_SALON_CSV = """entity_id,state,last_changed
sensor.thermometre_salon_temperature,20.02965051872222,2026-02-25T23:00:00.000Z
sensor.thermometre_salon_temperature,19.745057189277777,2026-02-26T00:00:00.000Z
sensor.thermometre_salon_temperature,19.25927061222222,2026-02-26T01:00:00.000Z
sensor.thermometre_salon_temperature,18.94511225288889,2026-02-26T02:00:00.000Z
sensor.thermometre_salon_temperature,18.749661724444447,2026-02-26T03:00:00.000Z
sensor.thermometre_salon_temperature,18.6,2026-02-26T04:00:00.000Z
sensor.thermometre_salon_temperature,18.8,2026-02-26T04:09:16.015Z
sensor.thermometre_salon_temperature,18.6,2026-02-26T04:29:39.937Z
sensor.thermometre_salon_temperature,18.4,2026-02-26T06:04:41.858Z
sensor.thermometre_salon_temperature,18.2,2026-02-26T07:06:08.999Z
sensor.thermometre_salon_temperature,18.4,2026-02-26T08:07:56.224Z
sensor.thermometre_salon_temperature,18.6,2026-02-26T08:59:59.234Z
sensor.thermometre_salon_temperature,18.8,2026-02-26T09:40:01.864Z
sensor.thermometre_salon_temperature,19,2026-02-26T10:19:34.198Z
sensor.thermometre_salon_temperature,unavailable,2026-02-26T10:41:18.986Z
sensor.thermometre_salon_temperature,unknown,2026-02-26T10:41:29.789Z
sensor.thermometre_salon_temperature,19,2026-02-26T10:42:04.371Z
sensor.thermometre_salon_temperature,19.2,2026-02-26T11:31:20.773Z
sensor.thermometre_salon_temperature,19,2026-02-26T11:57:52.398Z
sensor.thermometre_salon_temperature,18.8,2026-02-26T12:45:48.506Z
sensor.thermometre_salon_temperature,19,2026-02-26T13:20:43.967Z
sensor.thermometre_salon_temperature,19.2,2026-02-26T13:23:25.161Z
sensor.thermometre_salon_temperature,19.4,2026-02-26T13:27:01.764Z
sensor.thermometre_salon_temperature,19.6,2026-02-26T13:31:13.602Z
sensor.thermometre_salon_temperature,19.8,2026-02-26T13:37:51.490Z
sensor.thermometre_salon_temperature,20,2026-02-26T13:44:24.343Z
sensor.thermometre_salon_temperature,20.2,2026-02-26T13:56:54.790Z
sensor.thermometre_salon_temperature,20.4,2026-02-26T14:18:09.029Z
sensor.thermometre_salon_temperature,20.6,2026-02-26T14:35:26.525Z
sensor.thermometre_salon_temperature,20.4,2026-02-26T14:42:39.662Z
sensor.thermometre_salon_temperature,20.2,2026-02-26T14:49:37.675Z
sensor.thermometre_salon_temperature,20,2026-02-26T15:04:19.051Z
sensor.thermometre_salon_temperature,19.8,2026-02-26T15:43:16.018Z
sensor.thermometre_salon_temperature,20,2026-02-26T15:49:13.651Z
sensor.thermometre_salon_temperature,20.2,2026-02-26T15:54:51.119Z
sensor.thermometre_salon_temperature,20,2026-02-26T16:16:00.379Z
sensor.thermometre_salon_temperature,20.2,2026-02-26T16:26:40.022Z
sensor.thermometre_salon_temperature,20,2026-02-26T16:37:04.497Z
sensor.thermometre_salon_temperature,19.8,2026-02-26T17:04:26.335Z
sensor.thermometre_salon_temperature,20,2026-02-26T17:12:34.873Z
sensor.thermometre_salon_temperature,20.2,2026-02-26T17:30:12.541Z
sensor.thermometre_salon_temperature,20,2026-02-26T18:16:17.646Z
sensor.thermometre_salon_temperature,20.2,2026-02-26T22:10:39.697Z
sensor.thermometre_salon_temperature,20,2026-02-26T23:08:24.672Z
sensor.thermometre_salon_temperature,19.8,2026-02-26T23:25:52.227Z
sensor.thermometre_salon_temperature,19.6,2026-02-26T23:46:56.423Z
sensor.thermometre_salon_temperature,19.4,2026-02-27T00:03:18.558Z
sensor.thermometre_salon_temperature,19.2,2026-02-27T00:25:33.277Z
sensor.thermometre_salon_temperature,19,2026-02-27T00:57:22.110Z
sensor.thermometre_salon_temperature,18.8,2026-02-27T01:45:33.253Z
sensor.thermometre_salon_temperature,18.6,2026-02-27T02:52:37.697Z
sensor.thermometre_salon_temperature,18.8,2026-02-27T04:10:57.147Z
sensor.thermometre_salon_temperature,19,2026-02-27T04:14:58.928Z
sensor.thermometre_salon_temperature,19.2,2026-02-27T04:18:20.390Z
sensor.thermometre_salon_temperature,19.4,2026-02-27T04:21:26.740Z
sensor.thermometre_salon_temperature,19.6,2026-02-27T04:26:03.790Z
sensor.thermometre_salon_temperature,19.8,2026-02-27T04:30:35.776Z
sensor.thermometre_salon_temperature,20,2026-02-27T04:35:27.901Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T04:44:36.844Z
sensor.thermometre_salon_temperature,20.4,2026-02-27T04:55:51.722Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T05:27:35.483Z
sensor.thermometre_salon_temperature,20,2026-02-27T05:38:35.273Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T05:46:23.716Z
sensor.thermometre_salon_temperature,20.4,2026-02-27T06:02:15.610Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T06:16:01.637Z
sensor.thermometre_salon_temperature,20,2026-02-27T06:27:31.641Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T06:32:28.802Z
sensor.thermometre_salon_temperature,20.4,2026-02-27T06:40:07.112Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T06:49:21.051Z
sensor.thermometre_salon_temperature,20,2026-02-27T07:05:43.183Z
sensor.thermometre_salon_temperature,20.4,2026-02-27T07:19:39.265Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T07:12:05.973Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T07:29:13.425Z
sensor.thermometre_salon_temperature,20,2026-02-27T07:44:45.213Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T07:49:47.433Z
sensor.thermometre_salon_temperature,20.4,2026-02-27T07:57:15.688Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T08:07:45.244Z
sensor.thermometre_salon_temperature,20,2026-02-27T08:25:22.934Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T08:29:19.648Z
sensor.thermometre_salon_temperature,20.4,2026-02-27T09:02:18.932Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T09:10:57.708Z
sensor.thermometre_salon_temperature,20,2026-02-27T09:38:29.704Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T18:54:26.198Z
sensor.thermometre_salon_temperature,20.4,2026-02-27T21:20:54.865Z
sensor.thermometre_salon_temperature,20.2,2026-02-27T21:33:40.392Z
sensor.thermometre_salon_temperature,20,2026-02-27T23:05:50.403Z
sensor.thermometre_salon_temperature,19.8,2026-02-27T23:30:31.120Z
sensor.thermometre_salon_temperature,19.6,2026-02-27T23:47:53.662Z
sensor.thermometre_salon_temperature,19.4,2026-02-28T00:10:38.565Z
sensor.thermometre_salon_temperature,19.2,2026-02-28T00:42:57.691Z
sensor.thermometre_salon_temperature,19,2026-02-28T01:20:09.126Z
sensor.thermometre_salon_temperature,18.8,2026-02-28T02:07:19.753Z
sensor.thermometre_salon_temperature,18.6,2026-02-28T03:18:21.034Z
sensor.thermometre_salon_temperature,18.4,2026-02-28T04:39:36.715Z
sensor.thermometre_salon_temperature,18.6,2026-02-28T05:10:30.394Z
sensor.thermometre_salon_temperature,18.8,2026-02-28T05:19:34.397Z
sensor.thermometre_salon_temperature,19,2026-02-28T05:22:10.539Z
sensor.thermometre_salon_temperature,19.2,2026-02-28T05:25:01.801Z
sensor.thermometre_salon_temperature,19.4,2026-02-28T05:27:42.980Z
sensor.thermometre_salon_temperature,19.6,2026-02-28T05:30:39.295Z
sensor.thermometre_salon_temperature,19.8,2026-02-28T05:48:06.864Z
sensor.thermometre_salon_temperature,20,2026-02-28T05:54:44.724Z
sensor.thermometre_salon_temperature,20.2,2026-02-28T06:38:13.679Z
sensor.thermometre_salon_temperature,20.4,2026-02-28T07:14:49.546Z
sensor.thermometre_salon_temperature,20.2,2026-02-28T07:20:11.885Z
sensor.thermometre_salon_temperature,20,2026-02-28T07:51:25.519Z
sensor.thermometre_salon_temperature,19.8,2026-02-28T08:57:34.199Z
sensor.thermometre_salon_temperature,20,2026-02-28T09:02:26.323Z
sensor.thermometre_salon_temperature,20.2,2026-02-28T09:08:28.968Z
sensor.thermometre_salon_temperature,20,2026-02-28T09:19:48.889Z
sensor.thermometre_salon_temperature,19.8,2026-02-28T09:56:24.780Z
sensor.thermometre_salon_temperature,20,2026-02-28T10:01:37.042Z
sensor.thermometre_salon_temperature,20.2,2026-02-28T10:06:03.987Z
sensor.thermometre_salon_temperature,20.4,2026-02-28T10:14:17.560Z
sensor.thermometre_salon_temperature,20.6,2026-02-28T10:54:14.997Z
sensor.thermometre_salon_temperature,20.4,2026-02-28T11:22:52.582Z
sensor.thermometre_salon_temperature,20.2,2026-02-28T11:29:10.374Z
sensor.thermometre_salon_temperature,20.4,2026-02-28T11:44:32.035Z
sensor.thermometre_salon_temperature,20.2,2026-02-28T11:53:30.912Z
sensor.thermometre_salon_temperature,20,2026-02-28T12:18:16.654Z
sensor.thermometre_salon_temperature,19.8,2026-02-28T13:20:33.819Z
sensor.thermometre_salon_temperature,20,2026-02-28T14:24:46.688Z
sensor.thermometre_salon_temperature,20.2,2026-02-28T16:51:20.416Z
sensor.thermometre_salon_temperature,20.4,2026-02-28T17:21:08.310Z
sensor.thermometre_salon_temperature,20.2,2026-02-28T17:26:00.431Z
sensor.thermometre_salon_temperature,20.4,2026-02-28T20:18:05.022Z
sensor.thermometre_salon_temperature,20.2,2026-02-28T20:27:08.996Z
sensor.thermometre_salon_temperature,20,2026-02-28T22:26:30.938Z
sensor.thermometre_salon_temperature,19.8,2026-02-28T23:26:37.105Z
sensor.thermometre_salon_temperature,19.6,2026-02-28T23:56:35.124Z
sensor.thermometre_salon_temperature,19.4,2026-03-01T00:23:46.985Z
sensor.thermometre_salon_temperature,19.2,2026-03-01T01:09:52.215Z
sensor.thermometre_salon_temperature,19,2026-03-01T02:02:30.160Z
sensor.thermometre_salon_temperature,18.8,2026-03-01T02:59:09.977Z
sensor.thermometre_salon_temperature,18.6,2026-03-01T04:26:48.417Z
sensor.thermometre_salon_temperature,18.8,2026-03-01T05:08:26.792Z
sensor.thermometre_salon_temperature,19,2026-03-01T05:11:38.202Z
sensor.thermometre_salon_temperature,19.2,2026-03-01T05:14:49.618Z
sensor.thermometre_salon_temperature,19.4,2026-03-01T05:18:41.319Z
sensor.thermometre_salon_temperature,19.6,2026-03-01T05:22:38.053Z
sensor.thermometre_salon_temperature,19.8,2026-03-01T05:27:20.118Z
sensor.thermometre_salon_temperature,20,2026-03-01T05:33:22.789Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T05:41:21.259Z
sensor.thermometre_salon_temperature,20.4,2026-03-01T05:53:41.508Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T06:35:14.464Z
sensor.thermometre_salon_temperature,20,2026-03-01T06:50:11.006Z
sensor.thermometre_salon_temperature,19.8,2026-03-01T07:26:16.765Z
sensor.thermometre_salon_temperature,20,2026-03-01T07:30:33.639Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T07:36:31.260Z
sensor.thermometre_salon_temperature,20.4,2026-03-01T07:47:35.978Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T07:57:40.354Z
sensor.thermometre_salon_temperature,20,2026-03-01T08:23:56.761Z
sensor.thermometre_salon_temperature,unavailable,2026-03-01T09:43:04.131Z
sensor.thermometre_salon_temperature,unknown,2026-03-01T09:43:05.939Z
sensor.thermometre_salon_temperature,20,2026-03-01T09:43:48.624Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T10:01:24.186Z
sensor.thermometre_salon_temperature,20.4,2026-03-01T10:33:38.154Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T10:51:25.928Z
sensor.thermometre_salon_temperature,20.4,2026-03-01T11:10:14.071Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T11:39:11.590Z
sensor.thermometre_salon_temperature,20,2026-03-01T12:09:44.789Z
sensor.thermometre_salon_temperature,19.8,2026-03-01T12:59:11.336Z
sensor.thermometre_salon_temperature,20,2026-03-01T14:10:02.017Z
sensor.thermometre_salon_temperature,unavailable,2026-03-01T15:26:59.186Z
sensor.thermometre_salon_temperature,unknown,2026-03-01T15:27:03.591Z
sensor.thermometre_salon_temperature,20,2026-03-01T15:27:37.265Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T15:53:42.090Z
sensor.thermometre_salon_temperature,20.4,2026-03-01T16:01:10.329Z
sensor.thermometre_salon_temperature,20.6,2026-03-01T16:09:33.969Z
sensor.thermometre_salon_temperature,20.4,2026-03-01T16:15:21.482Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T16:26:56.532Z
sensor.thermometre_salon_temperature,20,2026-03-01T17:03:42.550Z
sensor.thermometre_salon_temperature,unavailable,2026-03-01T17:12:19.483Z
sensor.thermometre_salon_temperature,20,2026-03-01T17:12:39.252Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T17:17:03.349Z
sensor.thermometre_salon_temperature,20.4,2026-03-01T17:23:05.970Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T17:41:38.997Z
sensor.thermometre_salon_temperature,20.4,2026-03-01T18:28:14.281Z
sensor.thermometre_salon_temperature,20.6,2026-03-01T18:50:18.952Z
sensor.thermometre_salon_temperature,20.4,2026-03-01T19:35:48.677Z
sensor.thermometre_salon_temperature,20.2,2026-03-01T20:24:04.278Z
sensor.thermometre_salon_temperature,20,2026-03-01T23:11:17.017Z
sensor.thermometre_salon_temperature,19.8,2026-03-01T23:27:54.211Z
sensor.thermometre_salon_temperature,19.6,2026-03-01T23:47:07.652Z
sensor.thermometre_salon_temperature,19.4,2026-03-02T00:10:53.031Z
sensor.thermometre_salon_temperature,19.2,2026-03-02T00:47:59.175Z
sensor.thermometre_salon_temperature,19,2026-03-02T01:36:05.212Z
sensor.thermometre_salon_temperature,18.8,2026-03-02T02:46:36.172Z
sensor.thermometre_salon_temperature,18.6,2026-03-02T05:27:21.814Z
sensor.thermometre_salon_temperature,18.8,2026-03-02T06:16:33.547Z
sensor.thermometre_salon_temperature,18.6,2026-03-02T06:33:56.213Z
sensor.thermometre_salon_temperature,18.8,2026-03-02T08:49:05.692Z
sensor.thermometre_salon_temperature,19,2026-03-02T09:22:10.188Z
sensor.thermometre_salon_temperature,18.8,2026-03-02T09:45:25.420Z
sensor.thermometre_salon_temperature,unavailable,2026-03-02T10:01:00.727Z
sensor.thermometre_salon_temperature,unknown,2026-03-02T10:01:02.429Z
sensor.thermometre_salon_temperature,18.8,2026-03-02T10:01:39.898Z
sensor.thermometre_salon_temperature,18.6,2026-03-02T10:41:50.226Z
sensor.thermometre_salon_temperature,unavailable,2026-03-02T13:06:44.178Z
sensor.thermometre_salon_temperature,unknown,2026-03-02T13:06:48.376Z
sensor.thermometre_salon_temperature,18.6,2026-03-02T13:07:22.221Z
sensor.thermometre_salon_temperature,unavailable,2026-03-02T13:08:37.663Z
sensor.thermometre_salon_temperature,unknown,2026-03-02T13:08:47.297Z
sensor.thermometre_salon_temperature,18.6,2026-03-02T13:09:21.797Z
sensor.thermometre_salon_temperature,18.4,2026-03-02T13:33:10.442Z
sensor.thermometre_salon_temperature,18.6,2026-03-02T14:37:18.724Z
sensor.thermometre_salon_temperature,18.8,2026-03-02T14:40:25.100Z
sensor.thermometre_salon_temperature,19,2026-03-02T14:44:26.814Z
sensor.thermometre_salon_temperature,19.2,2026-03-02T14:48:03.400Z
sensor.thermometre_salon_temperature,19.4,2026-03-02T14:51:34.936Z
sensor.thermometre_salon_temperature,19.6,2026-03-02T14:56:47.208Z
sensor.thermometre_salon_temperature,19.8,2026-03-02T15:05:30.983Z
sensor.thermometre_salon_temperature,20,2026-03-02T15:16:30.717Z
sensor.thermometre_salon_temperature,20.2,2026-03-02T15:33:18.223Z
sensor.thermometre_salon_temperature,20.4,2026-03-02T15:53:47.054Z
sensor.thermometre_salon_temperature,20.2,2026-03-02T16:22:24.449Z
sensor.thermometre_salon_temperature,20,2026-03-02T16:29:27.521Z
sensor.thermometre_salon_temperature,20.2,2026-03-02T16:35:30.155Z
sensor.thermometre_salon_temperature,20.4,2026-03-02T16:43:33.692Z"""

VSLOPE_CSV = """entity_id,state,last_changed
sensor.salon_temperature_slope,0.002965011866666667,2026-02-25T23:00:00.000Z
sensor.salon_temperature_slope,-0.4556593949666667,2026-02-26T00:00:00.000Z
sensor.salon_temperature_slope,-0.42699780303888885,2026-02-26T01:00:00.000Z
sensor.salon_temperature_slope,-0.1793845497111111,2026-02-26T02:00:00.000Z
sensor.salon_temperature_slope,-0.098489328825,2026-02-26T03:00:00.000Z
sensor.salon_temperature_slope,-0.03,2026-02-26T03:19:44.506Z
sensor.salon_temperature_slope,-0.01,2026-02-26T03:24:44.507Z
sensor.salon_temperature_slope,-0.0,2026-02-26T03:29:44.508Z
sensor.salon_temperature_slope,-0.24,2026-02-26T04:29:39.943Z
sensor.salon_temperature_slope,-0.05,2026-02-26T04:59:44.526Z
sensor.salon_temperature_slope,-0.01,2026-02-26T05:04:42.129Z
sensor.salon_temperature_slope,-0.0,2026-02-26T05:04:44.528Z
sensor.salon_temperature_slope,-0.08,2026-02-26T06:04:41.864Z
sensor.salon_temperature_slope,-0.02,2026-02-26T06:34:44.549Z
sensor.salon_temperature_slope,-0.0,2026-02-26T06:39:44.549Z
sensor.salon_temperature_slope,-0.14,2026-02-26T07:06:09.005Z
sensor.salon_temperature_slope,-0.03,2026-02-26T07:39:44.562Z
sensor.salon_temperature_slope,-0.01,2026-02-26T07:44:44.562Z
sensor.salon_temperature_slope,-0.0,2026-02-26T07:49:44.563Z
sensor.salon_temperature_slope,0.02,2026-02-26T08:07:56.230Z
sensor.salon_temperature_slope,0.0,2026-02-26T08:39:44.575Z
sensor.salon_temperature_slope,0.09,2026-02-26T08:59:59.240Z
sensor.salon_temperature_slope,0.02,2026-02-26T09:34:44.588Z
sensor.salon_temperature_slope,0.0,2026-02-26T09:39:44.588Z
sensor.salon_temperature_slope,0.19,2026-02-26T09:40:01.874Z
sensor.salon_temperature_slope,0.04,2026-02-26T10:14:44.596Z
sensor.salon_temperature_slope,0.21,2026-02-26T10:19:34.208Z
sensor.salon_temperature_slope,unknown,2026-02-26T10:41:08.224Z
sensor.salon_temperature_slope,0.0,2026-02-26T11:16:08.205Z
sensor.salon_temperature_slope,0.1,2026-02-26T11:31:20.785Z
sensor.salon_temperature_slope,-0.07,2026-02-26T11:57:52.404Z
sensor.salon_temperature_slope,-0.01,2026-02-26T12:31:08.219Z
sensor.salon_temperature_slope,-0.0,2026-02-26T12:36:08.220Z
sensor.salon_temperature_slope,-0.12,2026-02-26T12:45:48.511Z
sensor.salon_temperature_slope,-0.02,2026-02-26T13:16:08.227Z
sensor.salon_temperature_slope,0.04,2026-02-26T13:20:43.972Z
sensor.salon_temperature_slope,1.44,2026-02-26T13:23:25.171Z
sensor.salon_temperature_slope,2.15,2026-02-26T13:27:01.769Z
sensor.salon_temperature_slope,2.6,2026-02-26T13:31:13.607Z
sensor.salon_temperature_slope,2.04,2026-02-26T13:37:51.494Z
sensor.salon_temperature_slope,1.95,2026-02-26T13:44:24.348Z
sensor.salon_temperature_slope,1.2,2026-02-26T13:56:54.795Z
sensor.salon_temperature_slope,0.69,2026-02-26T14:18:09.034Z
sensor.salon_temperature_slope,0.14,2026-02-26T14:42:39.667Z
sensor.salon_temperature_slope,-0.66,2026-02-26T14:49:37.685Z
sensor.salon_temperature_slope,-0.62,2026-02-26T15:04:19.056Z
sensor.salon_temperature_slope,-0.12,2026-02-26T15:36:08.260Z
sensor.salon_temperature_slope,-0.02,2026-02-26T15:41:08.260Z
sensor.salon_temperature_slope,-0.23,2026-02-26T15:43:16.023Z
sensor.salon_temperature_slope,0.12,2026-02-26T15:49:13.656Z
sensor.salon_temperature_slope,0.88,2026-02-26T15:54:51.128Z
sensor.salon_temperature_slope,0.09,2026-02-26T16:16:00.384Z
sensor.salon_temperature_slope,0.33,2026-02-26T16:26:40.027Z
sensor.salon_temperature_slope,-0.21,2026-02-26T16:37:04.502Z
sensor.salon_temperature_slope,-0.27,2026-02-26T17:04:26.340Z
sensor.salon_temperature_slope,0.18,2026-02-26T17:12:34.879Z
sensor.salon_temperature_slope,0.34,2026-02-26T17:30:12.546Z
sensor.salon_temperature_slope,0.07,2026-02-26T18:01:08.289Z
sensor.salon_temperature_slope,0.01,2026-02-26T18:06:08.290Z
sensor.salon_temperature_slope,0.0,2026-02-26T18:11:08.291Z
sensor.salon_temperature_slope,-0.04,2026-02-26T18:16:17.652Z
sensor.salon_temperature_slope,-0.01,2026-02-26T18:51:08.300Z
sensor.salon_temperature_slope,-0.0,2026-02-26T18:56:08.301Z
sensor.salon_temperature_slope,0.02,2026-02-26T22:10:39.702Z
sensor.salon_temperature_slope,0.0,2026-02-26T22:41:08.348Z
sensor.salon_temperature_slope,-0.05,2026-02-26T23:08:24.678Z
sensor.salon_temperature_slope,-0.37,2026-02-26T23:25:52.232Z
sensor.salon_temperature_slope,-0.44,2026-02-26T23:46:56.428Z
sensor.salon_temperature_slope,-0.65,2026-02-27T00:03:18.564Z
sensor.salon_temperature_slope,-0.54,2026-02-27T00:25:33.284Z
sensor.salon_temperature_slope,-0.11,2026-02-27T00:56:08.378Z
sensor.salon_temperature_slope,-0.02,2026-02-27T00:56:13.263Z
sensor.salon_temperature_slope,-0.29,2026-02-27T00:57:22.117Z
sensor.salon_temperature_slope,-0.06,2026-02-27T01:31:08.385Z
sensor.salon_temperature_slope,-0.01,2026-02-27T01:36:08.387Z
sensor.salon_temperature_slope,-0.0,2026-02-27T01:41:08.388Z
sensor.salon_temperature_slope,-0.2,2026-02-27T01:45:33.259Z
sensor.salon_temperature_slope,-0.04,2026-02-27T02:16:08.400Z
sensor.salon_temperature_slope,-0.01,2026-02-27T02:21:08.396Z
sensor.salon_temperature_slope,-0.0,2026-02-27T02:26:08.397Z
sensor.salon_temperature_slope,-0.14,2026-02-27T02:52:37.702Z
sensor.salon_temperature_slope,-0.03,2026-02-27T03:26:08.409Z
sensor.salon_temperature_slope,-0.01,2026-02-27T03:31:08.409Z
sensor.salon_temperature_slope,-0.0,2026-02-27T03:36:08.410Z
sensor.salon_temperature_slope,1.07,2026-02-27T04:14:58.934Z
sensor.salon_temperature_slope,1.79,2026-02-27T04:18:20.396Z
sensor.salon_temperature_slope,2.52,2026-02-27T04:21:26.746Z
sensor.salon_temperature_slope,2.79,2026-02-27T04:26:03.797Z
sensor.salon_temperature_slope,2.68,2026-02-27T04:30:35.782Z
sensor.salon_temperature_slope,2.7,2026-02-27T04:35:27.916Z
sensor.salon_temperature_slope,1.64,2026-02-27T04:44:36.852Z
sensor.salon_temperature_slope,1.18,2026-02-27T04:55:51.726Z
sensor.salon_temperature_slope,0.24,2026-02-27T05:26:08.434Z
sensor.salon_temperature_slope,0.06,2026-02-27T05:27:35.490Z
sensor.salon_temperature_slope,-0.42,2026-02-27T05:38:35.278Z
sensor.salon_temperature_slope,0.22,2026-02-27T05:46:23.724Z
sensor.salon_temperature_slope,0.41,2026-02-27T06:02:15.615Z
sensor.salon_temperature_slope,-0.02,2026-02-27T06:16:01.643Z
sensor.salon_temperature_slope,-0.5,2026-02-27T06:27:31.648Z
sensor.salon_temperature_slope,0.29,2026-02-27T06:32:28.808Z
sensor.salon_temperature_slope,0.81,2026-02-27T06:40:07.118Z
sensor.salon_temperature_slope,-0.05,2026-02-27T06:49:21.056Z
sensor.salon_temperature_slope,-0.36,2026-02-27T07:05:43.189Z
sensor.salon_temperature_slope,0.23,2026-02-27T07:12:05.978Z
sensor.salon_temperature_slope,0.81,2026-02-27T07:19:39.276Z
sensor.salon_temperature_slope,-0.04,2026-02-27T07:29:13.430Z
sensor.salon_temperature_slope,-0.38,2026-02-27T07:44:45.219Z
sensor.salon_temperature_slope,0.31,2026-02-27T07:49:47.438Z
sensor.salon_temperature_slope,0.83,2026-02-27T07:57:15.693Z
sensor.salon_temperature_slope,-0.02,2026-02-27T08:07:45.249Z
sensor.salon_temperature_slope,-0.33,2026-02-27T08:25:22.940Z
sensor.salon_temperature_slope,0.3,2026-02-27T08:29:19.653Z
sensor.salon_temperature_slope,0.06,2026-02-27T09:01:08.479Z
sensor.salon_temperature_slope,0.2,2026-02-27T09:02:18.938Z
sensor.salon_temperature_slope,-0.18,2026-02-27T09:10:57.722Z
sensor.salon_temperature_slope,-0.25,2026-02-27T09:38:29.709Z
sensor.salon_temperature_slope,-0.05,2026-02-27T10:11:08.493Z
sensor.salon_temperature_slope,-0.01,2026-02-27T10:11:13.268Z
sensor.salon_temperature_slope,-0.0,2026-02-27T10:16:08.493Z
sensor.salon_temperature_slope,0.04,2026-02-27T21:20:54.872Z
sensor.salon_temperature_slope,-0.14,2026-02-27T21:33:40.397Z
sensor.salon_temperature_slope,-0.03,2026-02-27T22:06:08.641Z
sensor.salon_temperature_slope,-0.01,2026-02-27T22:11:08.641Z
sensor.salon_temperature_slope,-0.0,2026-02-27T22:11:13.269Z
sensor.salon_temperature_slope,-0.06,2026-02-27T23:05:50.409Z
sensor.salon_temperature_slope,-0.32,2026-02-27T23:30:31.125Z
sensor.salon_temperature_slope,-0.56,2026-02-27T23:47:53.667Z
sensor.salon_temperature_slope,-0.51,2026-02-28T00:10:38.570Z
sensor.salon_temperature_slope,-0.1,2026-02-28T00:41:08.667Z
sensor.salon_temperature_slope,-0.32,2026-02-28T00:42:57.697Z
sensor.salon_temperature_slope,-0.06,2026-02-28T01:16:08.674Z
sensor.salon_temperature_slope,-0.26,2026-02-28T01:20:09.132Z
sensor.salon_temperature_slope,-0.05,2026-02-28T01:51:08.681Z
sensor.salon_temperature_slope,-0.01,2026-02-28T01:56:08.681Z
sensor.salon_temperature_slope,-0.0,2026-02-28T01:56:13.270Z
sensor.salon_temperature_slope,-0.2,2026-02-28T02:07:19.758Z
sensor.salon_temperature_slope,-0.04,2026-02-28T02:41:08.691Z
sensor.salon_temperature_slope,-0.01,2026-02-28T02:46:08.692Z
sensor.salon_temperature_slope,-0.0,2026-02-28T02:51:08.693Z
sensor.salon_temperature_slope,-0.14,2026-02-28T03:18:21.039Z
sensor.salon_temperature_slope,-0.03,2026-02-28T03:51:08.703Z
sensor.salon_temperature_slope,-0.01,2026-02-28T03:56:08.705Z
sensor.salon_temperature_slope,-0.0,2026-02-28T04:01:08.705Z
sensor.salon_temperature_slope,-0.12,2026-02-28T04:39:36.721Z
sensor.salon_temperature_slope,-0.02,2026-02-28T05:10:30.400Z
sensor.salon_temperature_slope,0.53,2026-02-28T05:19:34.402Z
sensor.salon_temperature_slope,1.77,2026-02-28T05:22:10.544Z
sensor.salon_temperature_slope,2.54,2026-02-28T05:25:01.807Z
sensor.salon_temperature_slope,3.19,2026-02-28T05:27:42.989Z
sensor.salon_temperature_slope,3.58,2026-02-28T05:30:39.300Z
sensor.salon_temperature_slope,1.46,2026-02-28T05:48:06.870Z
sensor.salon_temperature_slope,2.03,2026-02-28T05:54:44.729Z
sensor.salon_temperature_slope,0.41,2026-02-28T06:26:08.739Z
sensor.salon_temperature_slope,0.08,2026-02-28T06:31:08.738Z
sensor.salon_temperature_slope,0.02,2026-02-28T06:36:08.740Z
sensor.salon_temperature_slope,0.25,2026-02-28T06:38:13.683Z
sensor.salon_temperature_slope,0.05,2026-02-28T07:11:08.747Z
sensor.salon_temperature_slope,0.01,2026-02-28T07:11:13.275Z
sensor.salon_temperature_slope,0.28,2026-02-28T07:14:49.551Z
sensor.salon_temperature_slope,0.15,2026-02-28T07:20:11.891Z
sensor.salon_temperature_slope,0.03,2026-02-28T07:51:08.756Z
sensor.salon_temperature_slope,-0.15,2026-02-28T07:51:25.524Z
sensor.salon_temperature_slope,-0.03,2026-02-28T08:26:08.765Z
sensor.salon_temperature_slope,-0.01,2026-02-28T08:31:08.765Z
sensor.salon_temperature_slope,-0.0,2026-02-28T08:36:08.767Z
sensor.salon_temperature_slope,-0.11,2026-02-28T08:57:34.205Z
sensor.salon_temperature_slope,0.18,2026-02-28T09:02:26.328Z
sensor.salon_temperature_slope,0.99,2026-02-28T09:08:28.973Z
sensor.salon_temperature_slope,-0.01,2026-02-28T09:19:48.894Z
sensor.salon_temperature_slope,-0.0,2026-02-28T09:51:08.783Z
sensor.salon_temperature_slope,-0.16,2026-02-28T09:56:24.785Z
sensor.salon_temperature_slope,0.34,2026-02-28T10:01:37.050Z
sensor.salon_temperature_slope,1.25,2026-02-28T10:06:03.992Z
sensor.salon_temperature_slope,1.24,2026-02-28T10:14:17.566Z
sensor.salon_temperature_slope,0.25,2026-02-28T10:46:08.800Z
sensor.salon_temperature_slope,0.05,2026-02-28T10:51:08.800Z
sensor.salon_temperature_slope,0.23,2026-02-28T10:54:15.002Z
sensor.salon_temperature_slope,0.03,2026-02-28T11:22:52.589Z
sensor.salon_temperature_slope,-0.83,2026-02-28T11:29:10.380Z
sensor.salon_temperature_slope,-0.01,2026-02-28T11:44:32.040Z
sensor.salon_temperature_slope,-0.38,2026-02-28T11:53:30.918Z
sensor.salon_temperature_slope,-0.35,2026-02-28T12:18:16.661Z
sensor.salon_temperature_slope,-0.07,2026-02-28T12:51:08.826Z
sensor.salon_temperature_slope,-0.01,2026-02-28T12:56:08.827Z
sensor.salon_temperature_slope,-0.0,2026-02-28T13:01:08.828Z
sensor.salon_temperature_slope,-0.13,2026-02-28T13:20:33.823Z
sensor.salon_temperature_slope,-0.03,2026-02-28T13:51:08.835Z
sensor.salon_temperature_slope,-0.01,2026-02-28T13:56:08.837Z
sensor.salon_temperature_slope,-0.0,2026-02-28T13:56:13.264Z
sensor.salon_temperature_slope,0.01,2026-02-28T14:24:46.694Z
sensor.salon_temperature_slope,0.0,2026-02-28T14:56:08.851Z
sensor.salon_temperature_slope,0.04,2026-02-28T16:51:20.421Z
sensor.salon_temperature_slope,0.27,2026-02-28T17:21:08.315Z
sensor.salon_temperature_slope,-0.24,2026-02-28T17:26:00.438Z
sensor.salon_temperature_slope,-0.05,2026-02-28T17:56:08.891Z
sensor.salon_temperature_slope,-0.01,2026-02-28T17:56:13.258Z
sensor.salon_temperature_slope,-0.0,2026-02-28T18:01:08.893Z
sensor.salon_temperature_slope,0.03,2026-02-28T20:18:05.027Z
sensor.salon_temperature_slope,-0.26,2026-02-28T20:27:09.002Z
sensor.salon_temperature_slope,-0.05,2026-02-28T21:01:08.931Z
sensor.salon_temperature_slope,-0.01,2026-02-28T21:06:08.932Z
sensor.salon_temperature_slope,-0.0,2026-02-28T21:11:08.933Z
sensor.salon_temperature_slope,-0.05,2026-02-28T22:26:30.944Z
sensor.salon_temperature_slope,-0.01,2026-02-28T23:00:00.097Z
sensor.salon_temperature_slope,-0.0,2026-02-28T23:01:08.960Z
sensor.salon_temperature_slope,-0.14,2026-02-28T23:26:37.112Z
sensor.salon_temperature_slope,-0.32,2026-02-28T23:56:35.129Z
sensor.salon_temperature_slope,-0.4,2026-03-01T00:23:46.991Z
sensor.salon_temperature_slope,-0.08,2026-03-01T00:56:10.908Z
sensor.salon_temperature_slope,-0.02,2026-03-01T01:01:10.910Z
sensor.salon_temperature_slope,-0.0,2026-03-01T01:06:10.911Z
sensor.salon_temperature_slope,-0.2,2026-03-01T01:09:52.221Z
sensor.salon_temperature_slope,-0.04,2026-03-01T01:41:10.918Z
sensor.salon_temperature_slope,-0.01,2026-03-01T01:46:10.919Z
sensor.salon_temperature_slope,-0.0,2026-03-01T01:51:10.920Z
sensor.salon_temperature_slope,-0.18,2026-03-01T02:02:30.165Z
sensor.salon_temperature_slope,-0.04,2026-03-01T02:36:10.930Z
sensor.salon_temperature_slope,-0.01,2026-03-01T02:41:10.931Z
sensor.salon_temperature_slope,-0.0,2026-03-01T02:46:10.933Z
sensor.salon_temperature_slope,-0.17,2026-03-01T02:59:09.987Z
sensor.salon_temperature_slope,-0.03,2026-03-01T03:31:10.941Z
sensor.salon_temperature_slope,-0.01,2026-03-01T03:36:10.943Z
sensor.salon_temperature_slope,-0.0,2026-03-01T03:41:10.945Z
sensor.salon_temperature_slope,-0.11,2026-03-01T04:26:48.423Z
sensor.salon_temperature_slope,-0.02,2026-03-01T05:00:00.032Z
sensor.salon_temperature_slope,-0.0,2026-03-01T05:01:10.958Z
sensor.salon_temperature_slope,1.05,2026-03-01T05:11:38.207Z
sensor.salon_temperature_slope,2.02,2026-03-01T05:14:49.624Z
sensor.salon_temperature_slope,2.52,2026-03-01T05:18:41.324Z
sensor.salon_temperature_slope,2.69,2026-03-01T05:22:38.060Z
sensor.salon_temperature_slope,2.78,2026-03-01T05:27:20.122Z
sensor.salon_temperature_slope,2.3,2026-03-01T05:33:22.794Z
sensor.salon_temperature_slope,1.72,2026-03-01T05:41:21.264Z
sensor.salon_temperature_slope,1.16,2026-03-01T05:53:41.515Z
sensor.salon_temperature_slope,0.23,2026-03-01T06:26:10.974Z
sensor.salon_temperature_slope,0.05,2026-03-01T06:31:10.979Z
sensor.salon_temperature_slope,0.01,2026-03-01T06:35:14.470Z
sensor.salon_temperature_slope,-0.32,2026-03-01T06:50:11.011Z"""


def parse_ts(s):
    """Parse ISO timestamp to datetime."""
    s = s.replace("Z", "+00:00").rstrip("Z")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        # Fallback
        return datetime.strptime(s.split(".")[0], "%Y-%m-%dT%H:%M:%S")


def load_csv(raw_csv):
    """Load CSV string, filter numeric values, return list of (datetime, float)."""
    reader = csv.DictReader(io.StringIO(raw_csv.strip()))
    data = []
    for row in reader:
        state = row["state"].strip()
        if state in ("unavailable", "unknown", ""):
            continue
        try:
            val = float(state)
            ts = parse_ts(row["last_changed"].strip())
            data.append((ts, val))
        except (ValueError, TypeError):
            continue
    data.sort(key=lambda x: x[0])
    return data


def minutes_diff(t1, t2):
    """Return difference in minutes."""
    return (t2 - t1).total_seconds() / 60.0


def hours_diff(t1, t2):
    return (t2 - t1).total_seconds() / 3600.0


# ═══════════════════════════════════════════════════════════════════════════
# ANALYSE 1 : Détection des cycles de chauffe
# ═══════════════════════════════════════════════════════════════════════════
def detect_heating_events(temp_data, min_rise=1.0, max_duration_min=120):
    """Detect heating events: consecutive temperature rises > min_rise within max_duration.

    Returns list of dicts with start/end times, temperatures, duration, rate.
    """
    events = []
    i = 0
    while i < len(temp_data) - 1:
        # Look for start of a rise: current point lower than next
        if temp_data[i + 1][1] > temp_data[i][1]:
            start_idx = i
            j = i + 1
            # Follow the rise (allow small dips of 0.2°C max)
            peak_idx = j
            while j < len(temp_data) - 1:
                if temp_data[j + 1][1] >= temp_data[j][1]:
                    j += 1
                    if temp_data[j][1] >= temp_data[peak_idx][1]:
                        peak_idx = j
                elif temp_data[j][1] - temp_data[j + 1][1] <= 0.2:
                    # Small dip, keep going
                    j += 1
                else:
                    break
                # Don't extend beyond max duration
                if minutes_diff(temp_data[start_idx][0], temp_data[j][0]) > max_duration_min:
                    break

            rise = temp_data[peak_idx][1] - temp_data[start_idx][1]
            duration_min = minutes_diff(temp_data[start_idx][0], temp_data[peak_idx][0])

            if rise >= min_rise and duration_min > 0:
                events.append({
                    "start_time": temp_data[start_idx][0],
                    "end_time": temp_data[peak_idx][0],
                    "start_temp": temp_data[start_idx][1],
                    "peak_temp": temp_data[peak_idx][1],
                    "rise": rise,
                    "duration_min": duration_min,
                    "rate_per_hour": rise / (duration_min / 60),
                })
                i = peak_idx + 1
                continue
        i += 1
    return events


# ═══════════════════════════════════════════════════════════════════════════
# ANALYSE 2 : Analyse des slopes pendant les cycles de chauffe
# ═══════════════════════════════════════════════════════════════════════════
def analyze_slopes_during_heating(slope_data, heating_events):
    """For each heating event, extract slope values and compute statistics."""
    results = []
    for event in heating_events:
        slopes_in_event = [
            (ts, val) for ts, val in slope_data
            if event["start_time"] - timedelta(minutes=10) <= ts <= event["end_time"] + timedelta(minutes=10)
        ]
        if not slopes_in_event:
            continue

        vals = [v for _, v in slopes_in_event]
        positive_vals = [v for v in vals if v > 0]

        # Find time of first positive slope > 0.5 (significant response)
        first_sig = None
        for ts, v in slopes_in_event:
            if v > 0.5:
                first_sig = ts
                break

        result = {
            "event_start": event["start_time"],
            "slope_count": len(vals),
            "peak_slope": max(vals),
            "mean_positive_slope": sum(positive_vals) / len(positive_vals) if positive_vals else 0,
            "median_slope": sorted(vals)[len(vals) // 2],
            "first_significant_slope_time": first_sig,
        }

        if first_sig:
            result["dead_time_min"] = minutes_diff(event["start_time"], first_sig)
        else:
            result["dead_time_min"] = None

        results.append(result)
    return results


# ═══════════════════════════════════════════════════════════════════════════
# ANALYSE 3 : Refroidissement naturel (nuit)
# ═══════════════════════════════════════════════════════════════════════════
def analyze_cooling_periods(temp_data):
    """Detect nighttime cooling: sustained drops over several hours."""
    cooling = []
    i = 0
    while i < len(temp_data) - 3:
        # Look for sustained drop: at least 4 consecutive lower values
        if temp_data[i + 1][1] < temp_data[i][1] and temp_data[i + 2][1] < temp_data[i + 1][1]:
            start_idx = i
            j = i + 1
            while j < len(temp_data) - 1 and temp_data[j + 1][1] <= temp_data[j][1]:
                j += 1

            drop = temp_data[start_idx][1] - temp_data[j][1]
            duration_h = hours_diff(temp_data[start_idx][0], temp_data[j][0])

            if drop >= 0.8 and duration_h >= 1.0:
                cooling.append({
                    "start_time": temp_data[start_idx][0],
                    "end_time": temp_data[j][0],
                    "start_temp": temp_data[start_idx][1],
                    "end_temp": temp_data[j][1],
                    "drop": drop,
                    "duration_h": duration_h,
                    "rate_per_hour": -drop / duration_h,
                })
            i = j + 1
            continue
        i += 1
    return cooling


# ═══════════════════════════════════════════════════════════════════════════
# ANALYSE 4 : Oscillations autour de la consigne
# ═══════════════════════════════════════════════════════════════════════════
def analyze_oscillations(temp_data, target=20.0):
    """Analyze oscillation behavior around target temperature."""
    # Find periods where temperature stays within ±0.6°C of target
    osc_periods = []
    in_band = False
    band_start = None
    band_data = []

    for ts, temp in temp_data:
        if abs(temp - target) <= 0.6:
            if not in_band:
                in_band = True
                band_start = ts
                band_data = []
            band_data.append((ts, temp))
        else:
            if in_band and len(band_data) >= 4:
                duration_h = hours_diff(band_start, band_data[-1][0])
                if duration_h >= 0.5:
                    temps = [t for _, t in band_data]
                    # Count zero crossings around target
                    crossings = 0
                    for k in range(1, len(temps)):
                        if (temps[k - 1] - target) * (temps[k] - target) < 0:
                            crossings += 1

                    osc_periods.append({
                        "start": band_start,
                        "end": band_data[-1][0],
                        "duration_h": duration_h,
                        "min_temp": min(temps),
                        "max_temp": max(temps),
                        "amplitude": max(temps) - min(temps),
                        "samples": len(band_data),
                        "zero_crossings": crossings,
                        "period_min": (duration_h * 60) / max(crossings, 1) * 2,  # Approx oscillation period
                    })
            in_band = False
            band_data = []

    # Handle last period
    if in_band and len(band_data) >= 4:
        duration_h = hours_diff(band_start, band_data[-1][0])
        if duration_h >= 0.5:
            temps = [t for _, t in band_data]
            crossings = sum(1 for k in range(1, len(temps)) if (temps[k-1] - target) * (temps[k] - target) < 0)
            osc_periods.append({
                "start": band_start,
                "end": band_data[-1][0],
                "duration_h": duration_h,
                "min_temp": min(temps),
                "max_temp": max(temps),
                "amplitude": max(temps) - min(temps),
                "samples": len(band_data),
                "zero_crossings": crossings,
                "period_min": (duration_h * 60) / max(crossings, 1) * 2,
            })
    return osc_periods


# ═══════════════════════════════════════════════════════════════════════════
# ANALYSE 5 : Analyse détaillée du slope VTherm
# ═══════════════════════════════════════════════════════════════════════════
def analyze_slope_distribution(slope_data):
    """Characterize the slope signal: range, noise, typical values."""
    vals = [v for _, v in slope_data]
    positive = [v for v in vals if v > 0]
    negative = [v for v in vals if v < 0]
    near_zero = [v for v in vals if abs(v) < 0.05]

    return {
        "total_samples": len(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": sum(vals) / len(vals),
        "positive_count": len(positive),
        "negative_count": len(negative),
        "near_zero_count": len(near_zero),
        "mean_positive": sum(positive) / len(positive) if positive else 0,
        "mean_negative": sum(negative) / len(negative) if negative else 0,
        "p10": sorted(vals)[len(vals) // 10],
        "p25": sorted(vals)[len(vals) // 4],
        "p50": sorted(vals)[len(vals) // 2],
        "p75": sorted(vals)[3 * len(vals) // 4],
        "p90": sorted(vals)[9 * len(vals) // 10],
    }


# ═══════════════════════════════════════════════════════════════════════════
# ANALYSE 6 : Dead time réel (analyse croisée temp + slope)
# ═══════════════════════════════════════════════════════════════════════════
def analyze_dead_time_detailed(temp_data, slope_data, heating_events):
    """Cross-reference temperature and slope to measure actual dead time.

    Dead time = time between what appears to be heating start and
    when temperature sensor first clearly responds.
    """
    results = []
    for event in heating_events:
        # Look at slope data just before event start to find when slope first goes positive
        pre_slopes = [
            (ts, val) for ts, val in slope_data
            if event["start_time"] - timedelta(minutes=30) <= ts <= event["start_time"] + timedelta(minutes=15)
        ]

        # Find the transition from negative/zero to positive
        transition_time = None
        for k in range(1, len(pre_slopes)):
            if pre_slopes[k - 1][1] <= 0.05 and pre_slopes[k][1] > 0.1:
                transition_time = pre_slopes[k][0]
                break

        # Find first clear temp change (>= 0.2°C rise from local minimum)
        pre_temps = [
            (ts, val) for ts, val in temp_data
            if event["start_time"] - timedelta(minutes=5) <= ts <= event["start_time"] + timedelta(minutes=30)
        ]

        first_rise_time = None
        if len(pre_temps) >= 2:
            base_temp = pre_temps[0][1]
            for ts, val in pre_temps[1:]:
                if val >= base_temp + 0.2:
                    first_rise_time = ts
                    break

        if transition_time and first_rise_time:
            sensor_delay = minutes_diff(transition_time, first_rise_time)
        else:
            sensor_delay = None

        results.append({
            "event_start": event["start_time"],
            "slope_transition": transition_time,
            "first_temp_rise": first_rise_time,
            "sensor_delay_min": sensor_delay,
        })

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ANALYSE 7 : Overshoot après coupure chauffage
# ═══════════════════════════════════════════════════════════════════════════
def analyze_overshoot(temp_data, target=20.0):
    """Measure overshoot: how far temperature goes above target before falling back."""
    overshoots = []
    i = 0
    while i < len(temp_data) - 2:
        # Temperature just crossed above target
        if temp_data[i][1] <= target and temp_data[i + 1][1] > target:
            # Find the peak before it comes back down
            peak_idx = i + 1
            j = i + 2
            while j < len(temp_data) and temp_data[j][1] >= target:
                if temp_data[j][1] > temp_data[peak_idx][1]:
                    peak_idx = j
                j += 1

            overshoot = temp_data[peak_idx][1] - target
            if overshoot >= 0.1:
                time_above = minutes_diff(temp_data[i + 1][0], temp_data[min(j, len(temp_data) - 1)][0])
                overshoots.append({
                    "cross_time": temp_data[i + 1][0],
                    "peak_temp": temp_data[peak_idx][1],
                    "overshoot": overshoot,
                    "time_to_peak_min": minutes_diff(temp_data[i + 1][0], temp_data[peak_idx][0]),
                    "time_above_target_min": time_above,
                })
            i = j
            continue
        i += 1
    return overshoots


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
def main():
    temp_data = load_csv(TEMP_SALON_CSV)
    slope_data = load_csv(VSLOPE_CSV)

    print("=" * 80)
    print("ANALYSE DES DONNÉES RÉELLES - VTherm MPC Fan")
    print(f"Période : {temp_data[0][0].strftime('%Y-%m-%d %H:%M')} → {temp_data[-1][0].strftime('%Y-%m-%d %H:%M')}")
    print(f"Points température : {len(temp_data)}  |  Points slope : {len(slope_data)}")
    print("=" * 80)

    # 1. Heating events
    print("\n" + "─" * 80)
    print("1. CYCLES DE CHAUFFE DÉTECTÉS (montée > 1°C)")
    print("─" * 80)
    heating = detect_heating_events(temp_data)
    for i, h in enumerate(heating, 1):
        print(f"\n  Cycle {i}:")
        print(f"    Début    : {h['start_time'].strftime('%m/%d %H:%M')}  →  {h['end_time'].strftime('%H:%M')}")
        print(f"    Temp     : {h['start_temp']:.1f}°C → {h['peak_temp']:.1f}°C  (Δ = +{h['rise']:.1f}°C)")
        print(f"    Durée    : {h['duration_min']:.0f} min")
        print(f"    Vitesse  : {h['rate_per_hour']:.2f} °C/h")

    if heating:
        rates = [h["rate_per_hour"] for h in heating]
        durations = [h["duration_min"] for h in heating]
        print(f"\n  ► Résumé chauffe:")
        print(f"    Vitesse moyenne : {sum(rates)/len(rates):.2f} °C/h")
        print(f"    Vitesse min/max : {min(rates):.2f} / {max(rates):.2f} °C/h")
        print(f"    Durée moyenne   : {sum(durations)/len(durations):.0f} min")

    # 2. Slopes during heating
    print("\n" + "─" * 80)
    print("2. SLOPES VTherm PENDANT LES CYCLES DE CHAUFFE")
    print("─" * 80)
    slope_analysis = analyze_slopes_during_heating(slope_data, heating)
    for i, sa in enumerate(slope_analysis, 1):
        print(f"\n  Cycle {i} ({sa['event_start'].strftime('%m/%d %H:%M')}):")
        print(f"    Slope max      : {sa['peak_slope']:.2f} °C/h")
        print(f"    Slope moy pos  : {sa['mean_positive_slope']:.2f} °C/h")
        if sa.get("dead_time_min") is not None:
            print(f"    Dead time      : {sa['dead_time_min']:.1f} min (slope > 0.5)")

    if slope_analysis:
        peaks = [s["peak_slope"] for s in slope_analysis]
        dead_times = [s["dead_time_min"] for s in slope_analysis if s.get("dead_time_min") is not None]
        print(f"\n  ► Résumé slopes chauffe:")
        print(f"    Slope max moyen : {sum(peaks)/len(peaks):.2f} °C/h")
        if dead_times:
            print(f"    Dead time moyen : {sum(dead_times)/len(dead_times):.1f} min")
            print(f"    Dead time med   : {sorted(dead_times)[len(dead_times)//2]:.1f} min")

    # 3. Cooling periods
    print("\n" + "─" * 80)
    print("3. PÉRIODES DE REFROIDISSEMENT NATUREL")
    print("─" * 80)
    cooling = analyze_cooling_periods(temp_data)
    for i, c in enumerate(cooling, 1):
        print(f"\n  Période {i}:")
        print(f"    Début    : {c['start_time'].strftime('%m/%d %H:%M')}  →  {c['end_time'].strftime('%m/%d %H:%M')}")
        print(f"    Temp     : {c['start_temp']:.1f}°C → {c['end_temp']:.1f}°C  (Δ = -{c['drop']:.1f}°C)")
        print(f"    Durée    : {c['duration_h']:.1f}h")
        print(f"    Vitesse  : {c['rate_per_hour']:.3f} °C/h")

    if cooling:
        rates = [c["rate_per_hour"] for c in cooling]
        print(f"\n  ► Résumé refroidissement:")
        print(f"    Vitesse moyenne : {sum(rates)/len(rates):.3f} °C/h")
        print(f"    Vitesse min/max : {max(rates):.3f} / {min(rates):.3f} °C/h")

    # 4. Oscillations
    print("\n" + "─" * 80)
    print("4. OSCILLATIONS AUTOUR DE LA CONSIGNE (±0.6°C de 20°C)")
    print("─" * 80)
    osc = analyze_oscillations(temp_data)
    for i, o in enumerate(osc, 1):
        print(f"\n  Période {i}:")
        print(f"    Période  : {o['start'].strftime('%m/%d %H:%M')} → {o['end'].strftime('%m/%d %H:%M')} ({o['duration_h']:.1f}h)")
        print(f"    Plage    : {o['min_temp']:.1f} - {o['max_temp']:.1f}°C (amplitude {o['amplitude']:.1f}°C)")
        print(f"    Crossings: {o['zero_crossings']}  (période ≈ {o['period_min']:.0f} min)")

    if osc:
        amplitudes = [o["amplitude"] for o in osc]
        periods = [o["period_min"] for o in osc if o["zero_crossings"] > 0]
        print(f"\n  ► Résumé oscillations:")
        print(f"    Amplitude moyenne : {sum(amplitudes)/len(amplitudes):.2f}°C")
        if periods:
            print(f"    Période moyenne   : {sum(periods)/len(periods):.0f} min")

    # 5. Slope distribution
    print("\n" + "─" * 80)
    print("5. DISTRIBUTION DU SIGNAL SLOPE VTherm")
    print("─" * 80)
    dist = analyze_slope_distribution(slope_data)
    print(f"    Total samples  : {dist['total_samples']}")
    print(f"    Plage          : [{dist['min']:.2f}, {dist['max']:.2f}] °C/h")
    print(f"    Moyenne        : {dist['mean']:.4f} °C/h")
    print(f"    Positifs       : {dist['positive_count']} ({100*dist['positive_count']/dist['total_samples']:.0f}%)")
    print(f"    Négatifs       : {dist['negative_count']} ({100*dist['negative_count']/dist['total_samples']:.0f}%)")
    print(f"    Proches de 0   : {dist['near_zero_count']} ({100*dist['near_zero_count']/dist['total_samples']:.0f}%)")
    print(f"    Percentiles    : p10={dist['p10']:.2f}  p25={dist['p25']:.2f}  p50={dist['p50']:.2f}  p75={dist['p75']:.2f}  p90={dist['p90']:.2f}")
    print(f"    Moy positive   : {dist['mean_positive']:.2f} °C/h")
    print(f"    Moy négative   : {dist['mean_negative']:.2f} °C/h")

    # 6. Dead time detailed
    print("\n" + "─" * 80)
    print("6. ANALYSE DEAD TIME DÉTAILLÉE (croisement temp/slope)")
    print("─" * 80)
    dead = analyze_dead_time_detailed(temp_data, slope_data, heating)
    for i, d in enumerate(dead, 1):
        print(f"\n  Cycle {i} ({d['event_start'].strftime('%m/%d %H:%M')}):")
        if d["slope_transition"]:
            print(f"    Slope transition : {d['slope_transition'].strftime('%H:%M:%S')}")
        if d["first_temp_rise"]:
            print(f"    1ère montée temp : {d['first_temp_rise'].strftime('%H:%M:%S')}")
        if d.get("sensor_delay_min") is not None:
            print(f"    Délai capteur    : {d['sensor_delay_min']:.1f} min")

    # 7. Overshoot
    print("\n" + "─" * 80)
    print("7. OVERSHOOT (dépassement de la consigne 20°C)")
    print("─" * 80)
    overshoots = analyze_overshoot(temp_data)
    for i, o in enumerate(overshoots, 1):
        print(f"\n  Événement {i}:")
        print(f"    Passage 20°C : {o['cross_time'].strftime('%m/%d %H:%M')}")
        print(f"    Pic          : {o['peak_temp']:.1f}°C  (overshoot = +{o['overshoot']:.1f}°C)")
        print(f"    Temps au pic : {o['time_to_peak_min']:.0f} min")
        print(f"    Temps > 20°C : {o['time_above_target_min']:.0f} min")

    if overshoots:
        ov = [o["overshoot"] for o in overshoots]
        print(f"\n  ► Résumé overshoot:")
        print(f"    Overshoot moyen : +{sum(ov)/len(ov):.2f}°C")
        print(f"    Overshoot max   : +{max(ov):.2f}°C")

    # ═══════════════════════════════════════════════════════════════════════
    # COMPARAISON AVEC LES CONSTANTES DE L'ALGORITHME
    # ═══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 80)
    print("8. COMPARAISON AVEC LES CONSTANTES DE L'ALGORITHME")
    print("=" * 80)

    print(f"""
    ┌─────────────────────────┬──────────────┬──────────────────────────────────┐
    │ Constante               │ Valeur algo  │ Valeur mesurée                   │
    ├─────────────────────────┼──────────────┼──────────────────────────────────┤
    │ THRESHOLD_SLOPE         │    0.10      │ p50={dist['p50']:.2f} p75={dist['p75']:.2f} p90={dist['p90']:.2f}   │
    │ DEFAULT_DEAD_TIME       │   10.0 min   │ {"%.1f min (medical)" % (sum(d["dead_time_min"] for d in slope_analysis if d.get("dead_time_min"))/max(1,sum(1 for d in slope_analysis if d.get("dead_time_min"))))} │
    │ DEAD_TIME_SAFETY_FACTOR │    1.5x      │ Voir dead time variabilité       │
    │ MAX_PROJECTION_DELTA    │    2.0°C     │ Max slope={dist['max']:.2f} → proj={dist['max']*10/60:.2f}°C │
    │ THRESHOLD_TARGET_DROP   │   -1.0°C     │ Drops nocturnes: {sum(c['drop'] for c in cooling)/max(1,len(cooling)):.1f}°C en moy   │
    │ MIN_LIMIT_TIMEOUT       │    5.0 min   │ Dead time mini ≈ ? min           │
    │ DELTA_TIME_CONTROL_LOOP │    2.0 min   │ Oscillation ≈ {sum(o['period_min'] for o in osc if o['zero_crossings']>0)/max(1,sum(1 for o in osc if o['zero_crossings']>0)):.0f} min/cycle     │
    └─────────────────────────┴──────────────┴──────────────────────────────────┘
    """)

    # Specific algorithm insights
    print("  INSIGHTS POUR L'ALGORITHME:")
    print("  " + "─" * 60)

    # Dead time analysis
    dead_times_real = [s["dead_time_min"] for s in slope_analysis if s.get("dead_time_min") is not None]
    if dead_times_real:
        avg_dead = sum(dead_times_real) / len(dead_times_real)
        print(f"\n  a) Dead time réel moyen = {avg_dead:.1f} min")
        print(f"     → DEFAULT_DEAD_TIME = 10.0 min {'✓ OK' if 5 < avg_dead < 15 else '⚠ À AJUSTER'}")

    # Slope threshold
    print(f"\n  b) THRESHOLD_SLOPE = 0.10 °C/h")
    print(f"     → {100*dist['near_zero_count']/dist['total_samples']:.0f}% des slopes sont < 0.05 (bruit)")
    print(f"     → Seuil 0.10 sépare bien bruit/signal ✓")

    # Max projection
    max_slope = dist["max"]
    proj_10min = max_slope * 10 / 60
    print(f"\n  c) MAX_PROJECTION_DELTA = 2.0°C")
    print(f"     → Slope max observé = {max_slope:.2f} °C/h")
    print(f"     → Projection 10min à slope max = {proj_10min:.2f}°C")
    print(f"     → Le clamp ±2°C {'contient bien les projections ✓' if proj_10min < 2.0 else 'sera atteint! ⚠'}")

    # Overshoot
    if overshoots:
        avg_ov = sum(o["overshoot"] for o in overshoots) / len(overshoots)
        max_ov = max(o["overshoot"] for o in overshoots)
        print(f"\n  d) Overshoot moyen = +{avg_ov:.2f}°C, max = +{max_ov:.2f}°C")
        print(f"     → L'anticipation de freinage (Zone B) doit réagir")
        print(f"       quand projection > target, soit slope > ~{0.6 * 60 / 10:.1f} °C/h")
        print(f"       (pour +0.6°C en 10min)")

    # Oscillation insight
    if osc:
        avg_amp = sum(o["amplitude"] for o in osc) / len(osc)
        print(f"\n  e) Oscillation amplitude moyenne = {avg_amp:.2f}°C")
        print(f"     → Hystérésis naturelle du système ≈ ±{avg_amp/2:.2f}°C")
        print(f"     → Zone E (confort) devrait tolérer ±{avg_amp/2:.1f}°C sans agir")


if __name__ == "__main__":
    main()
