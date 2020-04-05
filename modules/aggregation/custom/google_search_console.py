from database.connection import Connection
from database.bigquery import BigQuery
from utilities.configuration import Configuration
from utilities.exceptions import ConfigurationMissingError
from googleapiclient.discovery import build
from googleapiclient.discovery import Resource
from google.cloud.bigquery import LoadJobConfig, TimePartitioning, TimePartitioningType, TableReference
from google.cloud.bigquery.job import WriteDisposition
from google.oauth2 import service_account
from os.path import abspath
from datetime import date, timedelta
from pandas import DataFrame, concat


class _DataAlreadyExistError(Exception):
    pass


class GoogleSearchConsole:
    COLLECTION_NAME = 'google_search_console'
    ROW_LIMIT = 25000
    DEFAULT_DIMENSIONS = ['page', 'device', 'query', 'country']
    DEFAULT_SEARCHTYPES = ['web', 'image', 'video']
    DEFAULT_SEARCHTYPE = 'web'

    def __init__(self, configuration: Configuration, connection: Connection):
        self.configuration = configuration
        self.connection = connection
        self.bigquery = None

    def run(self):
        print('Running aggregation GSC Importer:')

        gsc_importer_config = self.configuration.aggregations.get_custom_configuration_aggregation(
            'google_search_console'
        )

        if 'properties' in gsc_importer_config.settings and type(gsc_importer_config.settings['properties']) is dict:
            for gsc_property, property_configurations in gsc_importer_config.settings['properties'].items():
                for property_configuration in property_configurations:
                    credentials = None

                    if 'credentials' in property_configuration and type(property_configuration['credentials']) is str:
                        credentials = service_account.Credentials.from_service_account_file(
                            abspath(property_configuration['credentials']),
                            scopes=['https://www.googleapis.com/auth/webmasters.readonly']
                        )

                    api_service = build('webmasters', 'v3', credentials=credentials)

                    if 'dateDaysAgo' in property_configuration and type(property_configuration['dateDaysAgo']) is int:
                        request_days_ago = property_configuration['dateDaysAgo']
                    else:
                        request_days_ago = 3

                    if 'dimensions' in property_configuration and type(property_configuration['dimensions']) is int:
                        dimensions = property_configuration['dimensions']
                    else:
                        dimensions = GoogleSearchConsole.DEFAULT_DIMENSIONS

                    if 'searchTypes' in property_configuration and type(property_configuration['searchTypes']) is list:
                        search_types = property_configuration['searchTypes']
                    else:
                        search_types = GoogleSearchConsole.DEFAULT_SEARCHTYPES

                    if 'aggregationType' in property_configuration and type(property_configuration['aggregationType']) is str:
                        aggregation_type = property_configuration['aggregationType']
                    else:
                        aggregation_type = ''

                    table_name = None
                    dataset_name = None

                    if 'bigquery' == gsc_importer_config.database:
                        if 'tablename' in property_configuration and type(property_configuration['tablename']) is str:
                            table_name = property_configuration['tablename']
                        else:
                            raise ConfigurationMissingError('Missing tablename for gsc import to bigquery')

                        if 'dataset' in property_configuration and type(property_configuration['dataset']) is str:
                            dataset_name = property_configuration['dataset']

                        if type(self.bigquery) is not BigQuery:
                            self.bigquery = self.connection.bigquery

                    self.import_property(
                        api_service,
                        gsc_property,
                        request_days_ago,
                        dimensions,
                        search_types,
                        aggregation_type,
                        gsc_importer_config.database,
                        table_name,
                        dataset_name
                    )

    def import_property(
            self,
            api_service: Resource,
            gsc_property: str,
            request_days_ago: int,
            dimensions: list,
            search_types: list,
            aggregation_type: str,
            database: str,
            table_name: str,
            dataset_name: str = None
    ):
        request_date = date.today() - timedelta(days=request_days_ago)
        table_reference = self.bigquery.table_reference(table_name, dataset_name)
        iteration_count = 0

        if 'bigquery' == database:
            if self._bigquery_check_has_existing_data(table_reference, request_date):
                return

        for search_type in search_types:
            while True:
                request = {
                    'startDate': request_date.strftime('%Y-%m-%d'),
                    'endDate': request_date.strftime('%Y-%m-%d'),
                    'searchType': search_type,
                    'dimensions': dimensions,
                    'rowLimit': GoogleSearchConsole.ROW_LIMIT,
                    'startRow': GoogleSearchConsole.ROW_LIMIT * iteration_count
                }

                if 0 < len(aggregation_type):
                    request['aggregationType'] = aggregation_type

                response = api_service.searchanalytics().query(siteUrl=gsc_property, body=request).execute()

                if 'rows' not in response:
                    break

                if 'bigquery' == database:
                    self.process_response_rows_for_bigquery(
                        gsc_property,
                        response['rows'],
                        request_date,
                        dimensions,
                        search_type,
                        table_reference
                    )
                else:
                    self.process_response_rows_for_mongodb(
                        gsc_property,
                        response['rows'],
                        request_date,
                        dimensions,
                        search_type
                    )

                if len(response['rows']) < GoogleSearchConsole.ROW_LIMIT:
                    break

                iteration_count = iteration_count + 1

    def process_response_rows_for_mongodb(
            self,
            gsc_property: str,
            rows: list,
            request_date: date,
            dimensions: list,
            search_type: str
    ):
        documents = []
        mongodb = self.connection.mongodb

        for row in rows:
            documents.append({
                'property': gsc_property,
                'date': request_date,
                'clicks': row['clicks'],
                'impressions': row['impressions'],
                'ctr': row['ctr'],
                'position': row['position'],
                'searchType': search_type,
                'dimensions': self._process_dimensions_column(row['keys'], dimensions),
            })

        mongodb.insert_documents(GoogleSearchConsole.COLLECTION_NAME, documents)

    def process_response_rows_for_bigquery(
            self,
            gsc_property: str,
            rows: list,
            request_date: date,
            dimensions: list,
            search_type: str,
            table_reference: TableReference
    ):
        original_dataframe = DataFrame.from_records(rows)

        dimension_columns = original_dataframe['keys'].astype(str).str.replace('[', '').str.replace(']', '')
        dimension_columns = dimension_columns.str.split(pat=',', expand=True, n=len(dimensions) - 1)
        dimension_columns.columns = dimensions

        for key, value in dimension_columns.items():
            dimension_columns[key] = value.str.strip().str.strip('\'')

        dimension_columns['key'] = original_dataframe['keys']

        processed_dataframe = concat([dimension_columns, original_dataframe], axis=1, join='inner')
        processed_dataframe = processed_dataframe.drop(['key', 'keys'], axis=1)

        processed_dataframe['date'] = request_date
        processed_dataframe['property'] = gsc_property
        processed_dataframe['searchType'] = search_type

        job_config = LoadJobConfig()
        job_config.destination = table_reference
        job_config.write_disposition = WriteDisposition.WRITE_APPEND
        job_config.time_partitioning = TimePartitioning(type_=TimePartitioningType.DAY, field='date')

        load_job = self.bigquery.client.load_table_from_dataframe(
            processed_dataframe,
            table_reference,
            job_config=job_config
        )

        load_job.result()

    def _bigquery_check_has_existing_data(
            self,
            table_reference: TableReference,
            request_date: date
    ) -> bool:
        if not self.bigquery.has_table(table_reference.table_id, table_reference.dataset_id):
            return False

        query_job = self.bigquery.query(
            'SELECT COUNT(*) FROM `' + table_reference.dataset_id + '.' + table_reference.table_id + '` ' +
            'WHERE date = "' + request_date.strftime('%Y-%m-%d') + '"'
        )

        count = 0

        for row in query_job.result():
            count = row[0]

        return 0 < count

    @staticmethod
    def _process_dimensions_column(dimension_column: list, dimensions_list: list):
        return {dimensions_list[index]: dimension for index, dimension in enumerate(dimension_column)}
