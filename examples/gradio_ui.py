"""A small Gradio client for an airline that implements the Flight Control API.

Run the airline separately, then start this UI with:
    uv run python -m examples.gradio_ui
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import gradio as gr
import httpx

from flightcontrol import catalog

API_URL = os.environ.get("FLIGHT_API_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.environ.get("FLIGHT_API_KEY", "toy-key-123")
PAYMENT_TOKEN = "tok_ok_gradio_demo"
AIRPORTS = [
    (f"{airport.city} ({airport.code})", airport.code)
    for airport in catalog.AIRPORT_BY_CODE.values()
]


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error") or response.text)
    return response.text


def search_flights(origin: str, destination: str, travel_date: str, passengers: int):
    if origin == destination:
        return [], gr.Dropdown(choices=[], value=None), "Choose two different airports."
    try:
        parsed_date = date.fromisoformat(travel_date)
    except ValueError:
        return [], gr.Dropdown(choices=[], value=None), "Enter a valid date in YYYY-MM-DD format."

    try:
        response = httpx.get(
            f"{API_URL}/search",
            params={
                "origin": origin,
                "destination": destination,
                "date": parsed_date.isoformat(),
                "passengers": passengers,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=10,
        )
    except httpx.RequestError as exc:
        return [], gr.Dropdown(choices=[], value=None), f"Could not connect to the airline: {exc}"

    if response.status_code != 200:
        return [], gr.Dropdown(choices=[], value=None), (
            f"Airline returned HTTP {response.status_code}: {_error_detail(response)}"
        )

    itineraries = response.json().get("itineraries", [])
    rows = []
    choices = []
    for itinerary in itineraries:
        itinerary_id = itinerary["itinerary_id"]
        stops = itinerary.get("stops", 0)
        price = itinerary["price_cents"] / 100
        rows.append([
            itinerary_id,
            "Nonstop" if stops == 0 else f"{stops} stop(s)",
            f"${price:.2f}",
            itinerary.get("departure", ""),
            itinerary.get("arrival", ""),
            itinerary.get("seats_available", 0),
        ])
        choices.append((f"{itinerary_id} — ${price:.2f}", itinerary_id))

    message = f"Found {len(rows)} itinerary/itineraries." if rows else "No flights found for that search."
    return rows, gr.Dropdown(choices=choices, value=choices[0][1] if choices else None), message


def book_flight(
    itinerary_id: str,
    passenger_names: str,
    passengers: int,
    current_booking_id: str,
) -> tuple[str, str]:
    if not itinerary_id:
        return "Search for flights and select an itinerary first.", current_booking_id
    names = [name.strip() for name in passenger_names.splitlines() if name.strip()]
    if len(names) != passengers:
        return f"Enter exactly {passengers} passenger name(s), one per line.", current_booking_id

    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        with httpx.Client(base_url=API_URL, headers=headers, timeout=10) as client:
            hold_response = client.post(
                "/holds",
                json={"itinerary_id": itinerary_id, "passengers": passengers},
            )
            if hold_response.status_code != 201:
                return (
                    f"Could not hold the seats (HTTP {hold_response.status_code}): "
                    f"{_error_detail(hold_response)}",
                    current_booking_id,
                )

            hold = hold_response.json()
            booking_response = client.post(
                "/bookings",
                json={
                    "hold_id": hold["hold_id"],
                    "passengers": [{"name": name} for name in names],
                    "payment_token": PAYMENT_TOKEN,
                    "idempotency_key": f"gradio-{uuid.uuid4().hex}",
                },
            )
    except httpx.RequestError as exc:
        return f"Could not connect to the airline: {exc}", current_booking_id

    if booking_response.status_code not in (200, 201):
        return (
            f"The seats were held, but booking failed (HTTP {booking_response.status_code}): "
            f"{_error_detail(booking_response)}",
            current_booking_id,
        )

    booking = booking_response.json()
    return (
        f"Booking confirmed: **{booking['booking_id']}**  \n"
        f"Total: **${booking['total_cents'] / 100:.2f}**  \n"
        f"Status: {booking['status']}",
        booking["booking_id"],
    )


def cancel_booking(booking_id: str) -> str:
    booking_id = booking_id.strip()
    if not booking_id:
        return "Enter a booking ID to cancel."

    try:
        response = httpx.delete(
            f"{API_URL}/bookings/{booking_id}",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=10,
        )
    except httpx.RequestError as exc:
        return f"Could not connect to the airline: {exc}"

    if response.status_code != 200:
        return (
            f"Could not cancel booking (HTTP {response.status_code}): "
            f"{_error_detail(response)}"
        )

    booking = response.json()
    return f"Booking **{booking_id}** status: **{booking.get('status', 'cancelled')}**."


with gr.Blocks(title="Flight Booking Demo") as demo:
    gr.Markdown("# Flight Booking Demo\nSearch flights and book an itinerary.")

    with gr.Row():
        origin = gr.Dropdown(AIRPORTS, label="From", value="BOI")
        destination = gr.Dropdown(AIRPORTS, label="To", value="JFK")
        travel_date = gr.Textbox(label="Travel date (YYYY-MM-DD)", value="2027-03-15")
        passengers = gr.Slider(label="Passengers", minimum=1, maximum=9, step=1, value=1)

    search_button = gr.Button("Search flights", variant="primary")
    search_status = gr.Markdown()
    results = gr.Dataframe(
        headers=["Itinerary", "Stops", "Price per passenger", "Departure", "Arrival", "Seats"],
        datatype=["str", "str", "str", "str", "str", "number"],
        interactive=False,
        label="Available flights",
    )
    selected_itinerary = gr.Dropdown(label="Choose an itinerary", choices=[])

    gr.Markdown("## Passenger details")
    passenger_names = gr.Textbox(
        label="Passenger names (one per line)",
        lines=3,
        placeholder="Ada Park",
    )
    book_button = gr.Button("Hold seats and book", variant="primary")
    booking_status = gr.Markdown()
    booking_id = gr.Textbox(
        label="Booking ID (use this to cancel)",
        placeholder="Filled in after a successful booking",
    )
    cancel_button = gr.Button("Cancel booking")
    cancellation_status = gr.Markdown()

    search_button.click(
        search_flights,
        inputs=[origin, destination, travel_date, passengers],
        outputs=[results, selected_itinerary, search_status],
    )
    book_button.click(
        book_flight,
        inputs=[selected_itinerary, passenger_names, passengers, booking_id],
        outputs=[booking_status, booking_id],
    )
    cancel_button.click(
        cancel_booking,
        inputs=booking_id,
        outputs=cancellation_status,
    )


if __name__ == "__main__":
    demo.launch()
