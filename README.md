## Optional Gradio booking UI

Start an airline API first, such as the toy airline from `examples/` on port 8080. 
```
uv run --with fastapi --with uvicorn uvicorn examples.toy_airline:app --port 8080
```
In another terminal, run:

```
uv run python -m examples.gradio_ui
```

Now you can open  http://127.0.0.1:7860 in your browser

To confirm the status of a booking, run the following in another terminal:
```
curl -H 'Authorization: Bearer toy-key-123' \
  http://localhost:8080/bookings/PASTE_BOOKING_ID_HERE
```


Open `http://127.0.0.1:7860` to search for flights, make a demo booking, and
cancel it using the booking ID returned by the airline.
The UI defaults to the toy airline's `toy-key-123` API key. To connect it to
another server, set `FLIGHT_API_URL` and `FLIGHT_API_KEY` before starting it;
for example, use `http://localhost:8080` and `dev-key` for the Docker Compose
airline.
