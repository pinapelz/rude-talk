import io
import zipfile
import requests
import pandas as pd
from bs4 import BeautifulSoup
from constants import MAP_ID
import urllib.parse
from collections import namedtuple

FoodChoice = namedtuple("FoodChoice", ["name", "description", "layer_name", "url"])


def create_dataframe_from_google_maps_kml():
    url = f"https://www.google.com/maps/d/kml?mid={MAP_ID}&forcekml=1"
    response = requests.get(url)
    response.raise_for_status()

    if response.content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(response.content)) as kmz:
            kml_text = kmz.read("doc.kml")
    else:
        kml_text = response.content

    soup = BeautifulSoup(kml_text, features="xml")

    network_links = soup.find_all("NetworkLink")
    if network_links and not soup.find_all("Placemark"):
        print("Detected NetworkLink stub! Fetching actual data from inner link...")
        inner_url = network_links[0].find("href").text.strip()
        inner_resp = requests.get(inner_url)
        if inner_resp.content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(inner_resp.content)) as kmz:
                kml_text = kmz.read("doc.kml")
        else:
            kml_text = inner_resp.content
        soup = BeautifulSoup(kml_text, features="xml")

    rows = []

    for folder in soup.find_all("Folder"):
        folder_name_tag = folder.find("name", recursive=False)
        layer_name = folder_name_tag.text.strip() if folder_name_tag else "Unnamed Layer"

        for pm in folder.find_all("Placemark"):
            name_tag = pm.find("name")
            desc_tag = pm.find("description")
            coord_tag = pm.find("coordinates")

            pin_name = name_tag.text.strip() if name_tag else "Unnamed"
            description = desc_tag.text.strip() if desc_tag else ""

            lat, lon, gmaps_url = None, None, None
            if coord_tag and coord_tag.text.strip():
                first_coord = coord_tag.text.strip().split()[0]
                parts = first_coord.split(",")
                if len(parts) >= 2:
                    lon, lat = float(parts[0]), float(parts[1])
                    encoded_name = urllib.parse.quote_plus(pin_name)
                    gmaps_url = f"https://www.google.com/maps/search/{encoded_name}/@{lat},{lon},19z"

            rows.append({
                "layer_name": layer_name,
                "Name": pin_name,
                "description": description,
                "lat": lat,
                "lon": lon,
                "google_maps_url": gmaps_url,
            })
    return pd.DataFrame(rows)


def pick_choice_from_layer(layer_name: str) -> FoodChoice:
    df = create_dataframe_from_google_maps_kml()
    random_row = df[df["layer_name"] == layer_name].sample(n=1)
    return FoodChoice(
        name=random_row["Name"].iloc[0],
        description=random_row["description"].iloc[0],
        url=random_row["google_maps_url"].iloc[0],
        layer_name=layer_name,
    )
