"""
Description : Ce script surveille plusieurs fichiers CSV pour détecter les modifications 
et envoie les métriques extraites vers une base de données InfluxDB. Il utilise la bibliothèque 
`watchdog` pour observer les changements dans les fichiers et traite les données en temps réel 
grâce à des threads et une file d'attente.

Fonctionnalités principales :
- Surveillance de plusieurs fichiers CSV pour détecter les modifications.
- Extraction des colonnes et conversion des valeurs en types appropriés (entiers, flottants, etc.).
- Agrégation des métriques et ajout d'informations sur la source des données.
- Envoi des données traitées à InfluxDB via l'API de son client.
- Gestion simultanée des fichiers et des connexions à la base de données à l'aide de threads.

Prérequis :
- Installez les bibliothèques nécessaires (`watchdog`, `influxdb_client`, etc.).
- Configurez les informations d'accès à votre instance InfluxDB (URL, jeton, organisation, bucket).
- Assurez-vous que les fichiers CSV à surveiller existent dans les chemins spécifiés.

Remarque importante : 
- Le nombre de fichiers CSV surveillés (nombre d'UE) correspond au nombre de chemins spécifiés dans la liste `file_paths`. 
  Pour surveiller plus ou moins de fichiers, modifiez cette liste en ajoutant ou supprimant des chemins de fichiers.
  Assurez-vous que chaque fichier correspond aux exigences du traitement (format CSV).

"""






import argparse
import json
import logging
import signal
import socket
import os
from contextlib import suppress
from datetime import datetime
from queue import Queue
from threading import Thread
from time import sleep
from typing import Any, Dict, Optional, Tuple

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from influxdb_client import InfluxDBClient, WriteApi
from influxdb_client.client.write_api import SYNCHRONOUS

# Configuration InfluxDB
bucket = "srsran11"
org = "srs1"
token = "605bc59413b7d5457d181ccf20f9fda15693f81b068d70396cc183081b264fbb"
url = "http://localhost:8087"  # ou l'URL de ton instance InfluxDB
username = "admin1"
password = "admin12345"


class FileWatcherHandler(FileSystemEventHandler):
    def __init__(self, file_path, queue, source_name):
         """
        Initialise un gestionnaire pour surveiller les modifications d'un fichier.
        """
        self.file_path = file_path
        self.queue = queue
        self.source_name = source_name
        self.last_position = 0
        self.columns = []
        self.partial_data = {}

    def on_modified(self, event):
        """
        Gère les modifications apportées au fichier surveillé.
        """
        if event.src_path == self.file_path:
            with open(self.file_path, 'r') as file:
                file.seek(self.last_position)
                lines = file.readlines()
                self.last_position = file.tell()

            if lines:
                for line in lines:
                    # Lecture des colonnes depuis la première ligne si elles ne sont pas encore initialisées
                    if not self.columns:
                        self.columns = line.strip().split(';')
                    else:
                        fields = line.strip().split(';')
                        data_dict = {col: self._convert_value(val) for col, val in zip(self.columns, fields) if val}

                        # Mise à jour des données partielles
                        self.partial_data.update(data_dict)

                        # Si toutes les métriques sont présentes, mettre à jour la file d'attente
                        if all(metric in self.partial_data for metric in self.columns):
                            self.partial_data["source"] = self.source_name  # Ajout de la source
                            self.queue.put((self.partial_data.copy(), self.columns))
                            self.partial_data.clear()  # Réinitialiser les données partielles

    def _convert_value(self, value):
        # Convertir les valeurs en leur type approprié
        if value.lower() == 'n/a':
            return None
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value

def send_to_influxdb(client, bucket, queue):
    """
    Envoie les données extraites à InfluxDB.
    """
    write_api = client.write_api(write_options=SYNCHRONOUS)
    while True:
        item = queue.get()
        if item is None:
            break

        data_dict, columns = item
        point = {
            "measurement": "ue_metrics",
            "tags": {
                "source": data_dict.pop("source")
            },
            "fields": {},
        }

        for key in ['dl_bler', 'ul_bler', 'dl_brate', 'ul_brate','rsrp']:
            if key in data_dict:
                value = data_dict[key]
                if isinstance(value, int):
                    value = float(value)
                point["fields"][key] = value

        logging.info(f"Point envoyé à InfluxDB : {point}")
        write_api.write(bucket=bucket, org=org, record=point)
        #logging.info(f"DataDict : {data_dict}")

def recreate_bucket(client: InfluxDBClient, bucket_name: str) -> None:
    """
    Supprime et recrée le bucket spécifié dans InfluxDB.
    """
    logging.info("Recreating the bucket")
    api = client.buckets_api()
    bucket_ref = api.find_bucket_by_name(bucket_name)
    if bucket_ref:
        api.delete_bucket(bucket_ref)
    api.create_bucket(bucket_name)
    logging.info("Bucket cleaned")

def main():
    # Liste des chemins de fichiers à surveiller
    file_paths = ["/tmp/ue1_metrics.csv", "/tmp/ue2_metrics.csv", "/tmp/ue3_metrics.csv"]
    log_level = logging.INFO
    clean_bucket = False
    
    # Connexion au client InfluxDB
    client = InfluxDBClient(url=url, token=token, org=org, username=username, password=password)

    logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=log_level)
    logging.info("Starting file watcher")

    if clean_bucket:
        recreate_bucket(client, bucket)

    queue_obj = Queue()

    observers = []
    threads = []
    
    # Initialisation des gestionnaires pour chaque fichier
    for i, file_path in enumerate(file_paths):
        source_name = f"ue{i+1}"
        file_watcher_handler = FileWatcherHandler(file_path, queue_obj, source_name)
        observer = Observer()
        observer.schedule(file_watcher_handler, path=os.path.dirname(file_path), recursive=False)
        observers.append(observer)

        thread = Thread(target=send_to_influxdb, args=(client, bucket, queue_obj))
        threads.append(thread)
    
    # Démarrer les observateurs
    for observer in observers:
        observer.start()

    def close():
        """
        Arrête les threads et observateurs proprement.
        """
        logging.info("Closing")
        queue_obj.put(None)
        for observer in observers:
            observer.stop()
        for observer in observers:
            observer.join()
    
    # Gestion des signaux pour arrêter le script proprement
    signal.signal(signal.SIGINT, lambda signum, frame: close())
    signal.signal(signal.SIGTERM, lambda signum, frame: close())
    
    # Démarrer les threads 
    for thread in threads:
        thread.start()

    for observer in observers:
        observer.join()
    for thread in threads:
        thread.join()
    logging.info("End")

if __name__ == "__main__":
    main()

