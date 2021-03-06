# Bot class file

import tweepy
import os
import time
import random
import datetime
import psycopg2
import boto3
import botocore
from urllib.parse import urlparse


class Bot:
    s3 = boto3.resource('s3')
    client = boto3.client('s3')
    
    def __init__(self, app_keys, bot_keys):
        """
        Constructor method for bots. app_keys and bot_keys are dictionary objects containing
        data from keys.json.
        """
        self.tweet_enabled = bot_keys['tweet_enabled']
        self.follow_back_enabled = bot_keys['follow_back_enabled']
        self.unfollow_enabled = bot_keys['unfollow_enabled']
        self.preload = bot_keys['preload']
        self.screen_name = bot_keys['screen_name']
        self.access_token = bot_keys['access_token']
        self.access_token_secret = bot_keys['access_token_secret']
        self.queue_table = bot_keys['queue_table']
        self.recent_queue_table = bot_keys['recent_queue_table']
        self.request_sent_table = bot_keys['request_sent_table']
        self.recent_limit = bot_keys['recent_limit']
        self.bucket_name = bot_keys['bucket_name']
        self.bucket_directory = bot_keys['bucket_directory']
        self.max_download_attempts = bot_keys['max_download_attempts']
        self.max_tweet_attempts = bot_keys['max_tweet_attempts']
        self.follower_retrieve_limit = bot_keys['follower_retrieve_limit']
        
        self.database_url = app_keys['database_url']
        self.tweet_timeout = app_keys['tweet_timeout']
        
        self.auth = tweepy.OAuthHandler(app_keys['consumer_key'], app_keys['consumer_secret'])
        self.auth.set_access_token(self.access_token, self.access_token_secret)
        self.auth.secure = True
        self.api = tweepy.API(self.auth, timeout=5)
        
    def tweet(self):
        """
        This function will attempt to download a file from S3 (up to max_download_attempts)
        to the local filesystem. Next, it will attempt to tweet the file. If successful, it
        will update the corresponding recent queue table with the latest file.    
        """
        tweet = self.download_latest()

        if tweet is not None:
            filepath = tweet['filepath']
            comment = tweet['comment']
            
            self.delete_row(self.queue_table, 'filepath', filepath)
        
            self.tweet_media(filepath, comment)
            
            # Push the tweeted file into the table of recent tweets, and remove the oldest entries
            # from the table until the limit is reached
            self.insert_recent(filepath)
            row_count = self.count_rows(self.recent_queue_table)
            if row_count > self.recent_limit:
                for i in range(row_count - self.recent_limit):
                    self.delete_oldest_row(self.recent_queue_table, 'timestamp')
                
            
    def download_latest(self):
        """
        Get the latest filepath from the appropriate queue and attempt to download the file
        from S3. Failure usually means that the file no longer exists in the bucket, so try
        the next filepath in the queue if the file fails to download.
        
        If the download was successful, return the filepath and comment.
        """
        for attempt in range(self.max_download_attempts):
            # Get the latest filepath from the queue, determine destination temp filepath
            row = self.get_newest_row(self.queue_table)
            filepath = row[0]
            comment = row[1]
            temp_file = os.path.abspath(filepath)
            dirname = os.path.dirname(temp_file)
            
            # Skip downloading if the file already exists locally
            if (os.path.isfile(temp_file)):
                return {'filepath': filepath, 'comment': comment}

            # Create folder of the destination temp file, otherwise download_file will fail
            # with a FileNotFoundError
            if not os.path.isdir(dirname):
                os.makedirs(dirname)

            # Download the file to the local filesystem. If the attempt failed, retry with
            # the next file in the queue.
            try:
                self.s3.meta.client.download_file(self.bucket_name, filepath, temp_file)
                return {'filepath': filepath, 'comment': comment}
            except FileNotFoundError as error:
                print("{0}: Could not download file, the destination folder does not exist.".format(self.screen_name))
                self.delete_row(self.queue_table, 'filepath', filepath)
                continue
            except botocore.exceptions.ClientError as error:
                print("{0}: Could not download file, the file does not exist in the bucket.".format(self.screen_name))
                self.delete_row(self.queue_table, 'filepath', filepath)
                continue
            except IsADirectoryError as error:
                print("{0}: There was an error when saving the file (attempted to download a folder instead of a file).".format(self.screen_name))
                self.delete_row(self.queue_table, 'filepath', filepath)
                break
                
        return None # If all three attempts fail, just return None
            
    def tweet_media(self, filepath, comment):
        # Takes an absolute file path to a media file and posts a tweet with the file.
        for attempt in range(self.max_tweet_attempts):
            try:
                # This uploads the file and receives a media_id value
                ids = []
                uploaded = self.api.media_upload(filepath)
                ids.append(uploaded['media_id'])

                # Use the media_id value to tweet the file
                self.api.update_status(status=comment, media_ids=ids)
                print("{0}: Tweeted file {1}".format(self.screen_name, os.path.basename(filepath)))

            except tweepy.error.TweepError as error:
                """
                Sometimes a file may still be tweeted even if the Twitter API returned an error. In
                this scenario, the bot will end up tweeting the same file again on a subsequent retry.
                A missed post is better than a double post, but we will attempt to try again if the
                error was thrown by the call to media_upload rather than update_status.
                
                This is under the assumption that if "not ids" evaluates to True (therefore ids is empty),
                then the error occurred while the file was uploading.
                
                """
                if error.response is not None:
                    if error.response.status_code == 429:
                        print("{0}: Could not tweet file. Request limit reached.".format(self.screen_name))
                    elif error.response.status_code == 500:
                        print("{0}: Could not tweet file. Twitter server error.".format(self.screen_name))
                        if not ids:
                            print("{0}: Attempting to tweet again.".format(self.screen_name))
                            continue
                    elif error.response.status_code == 503:
                        print("{0}: Could not tweet file. Service unavailable.".format(self.screen_name))
                        if not ids:
                            print("{0}: Attempting to tweet again.".format(self.screen_name))
                            continue
                    else:
                        print("{0}: Could not tweet file. Reason: {1} ({2})".format(self.screen_name, error.reason, error.response.status_code))
                        if not ids:
                            print("{0}: Attempting to tweet again.".format(self.screen_name))
                            continue
                else:
                    # Possible errors:
                    # "Failed to send request: HTTPSConnectionPool(host='upload.twitter.com', port=443): Read timed out"
                    # "Failed to send request: ('Connection aborted.', BrokenPipeError(32, 'Broken pipe'))"
                    # "Failed to send request: ('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
                    print("{0}: Something went very wrong. Reason: {1}".format(self.screen_name, error.reason))
                    if not ids:
                        print("{0}: Attempting to tweet again.".format(self.screen_name))
                        continue

            except TypeError as error:
                print("{0}: Could not tweet file. Uploading failed.".format(self.screen_name))
            
            break


    def follow_back(self):
        """
        Retrieves a follower list of length follower_retrieve_limit and checks with the database to see if a
        follow request has been sent to the user in the past. If not, send the user a follow request.
        
        A follow request will ONLY be sent if a request has not been sent already. Users with
        protected accounts have one chance to accept, and users who unfollow and follow again will
        not be sent a second follow request. If the table is cleared, users may receive another
        follow request.
        """
        try:
            # items() returns an iterator object. Copy the items from the iterator
            # into a regular list of followers.
            followers_iterator = tweepy.Cursor(self.api.followers).items(self.follower_retrieve_limit)
            followers = [follower for follower in followers_iterator]

            # Check if a follow request has already been sent, if not, then send a follow request
            for follower in followers:
                if not self.request_sent(follower.id_str):
                    try:
                        # Send the follow request
                        follower.follow()
                        self.update_request_sent(follower.id_str, follower.screen_name)
                        print("{0}: Follow request sent to {1}".format(self.screen_name, follower.screen_name))

                    except tweepy.error.TweepError as error:
                        if error.response is not None:
                            if error.response.status_code == 403:
                                # This error can occur if a previous follow request is sent to a protected account,
                                # and the request is still pending approval by the user. It can also occur if the
                                # user is blocking the account or if the user has been suspended.
                                #
                                # Add the user to the table of sent requests to prevent this error from occurring.
                                self.update_request_sent(follower.id_str, follower.screen_name)
                                print("{0}: Could not follow user {1}. {2}".format(self.screen_name, follower.screen_name, error.reason))
                            elif error.response.status_code == 429:
                                print("{0}: Could not follow user. Request limit reached.".format(self.screen_name))
                            else:
                                print("{0}: Could not follow user. Error status code {1}".format(self.screen_name, error.response.status_code))

        except tweepy.error.TweepError as error:
            if error.response is not None:
                if error.response.status_code == 429:
                    print("{0}: Could not follow user. Request limit reached.".format(self.screen_name))
                elif error.response.status_code == 500:
                    print("{0}: Could not follow user. Twitter server error.".format(self.screen_name))
                elif error.response.status_code == 503:
                    print("{0}: Could not follow user. Service unavailable.".format(self.screen_name))
                else:
                    print("{0}: Could not follow user. Error status code {1}".format(self.screen_name, error.response.status_code))
            else:
                print("{0}: Something went very wrong. Reason: {1}".format(self.screen_name, error.reason))


    def unfollow(self):
        """
        Retrieves a list of all friends and all followers, and checks for friends who are no
        longer following. Retrieval is broken into pages of 5000 users at maximum and will
        wait 60 seconds between pages if there is more than one page.
        
        Calls to GET users/lookup are rate limited to 180 requests in a 15 minute interval.
        unfollow() will be called on a timely basis, so it is unlikely the limit will be
        reached, but nevertheless, if the limit is reached, any users left over will be
        unfollowed on the next call to unfollow().
        
        Possible bug with tweepy? Incorrect friends_count.
        """
        try:
            # Grab list of users that the account is following (list of ids)
            friends = []
            for page in tweepy.Cursor(self.api.friends_ids).pages():
                friends.extend(page)
                
                if len(page) == 5000:
                    time.sleep(60)
                    
            # Grab list of users who follow the account (list of ids)
            followers = []
            for page in tweepy.Cursor(self.api.followers_ids).pages():
                followers.extend(page)
                
                if len(page) == 5000:
                    time.sleep(60)
                    
            not_following = 0
            
            # Check relationship status for each user and add them to a list if they are not following
            for friend in friends:
                if friend not in followers:
                    try:
                        user = self.api.get_user(friend)
                        user.unfollow()
                        self.delete_row(self.request_sent_table, 'id', user.id_str)
                        print("{0}: Unfollowed {1}".format(self.screen_name, user.screen_name))
                        
                        not_following += 1
                        if not_following >= 180:
                            break # Quit unfollowing if we hit the limit
                            
                    except tweepy.error.TweepError as error:
                        if error.response is not None:
                            if error.response.status_code == 403:
                                print("{0}: Could not unfollow user. {1}".format(self.screen_name, error.reason))
                            elif error.response.status_code == 429:
                                print("{0}: Could not unfollow user. Request limit reached.".format(self.screen_name))
                            else:
                                print("{0}: Could not unfollow user. Error status code {1}".format(self.screen_name, error.response.status_code))
                                
        except tweepy.error.TweepError as error:
            if error.response is not None:
                if error.response.status_code == 429:
                    print("{0}: Could not unfollow user. Request limit reached.".format(self.screen_name))
                elif error.response.status_code == 500:
                    print("{0}: Could not unfollow user. Twitter server error.".format(self.screen_name))
                elif error.response.status_code == 503:
                    print("{0}: Could not unfollow user. Service unavailable.".format(self.screen_name))
                else:
                    print("{0}: Could not unfollow user. Error status code {1}".format(self.screen_name, error.response.status_code))
            else:
                print("{0}: Something went very wrong. Reason: {1}".format(self.screen_name, error.reason))


    def smart_queue(self):
        """
        Randomly adds files to a queue table. However, this algorithm will
        attempt to ensure that the most recently posted files will not appear at the front
        of the queue. IMPORTANT: "front of the queue" in this instance means the newest files
        added to the table.

        The most recently posted files are kept in a table called [prefix]_recent_queue,
        where [prefix] is the screen_name of a twitter bot. The table length, at maximum,
        should be equal to a user limit defined in keys.json with the key "recent_limit".

        Valid files are drawn from the pool by excluding the recent files. This list is
        then shuffled, and a certain number (up to recent_limit) of those files are
        selected to be placed at the start of a list called new queue. The remaining
        files are mixed with the recent files to form the rest of the list.
        
        When building the new queue, the list is traversed in reverse order to make
        the aforementioned group of files go at the front of the queue.

        NOTE:
        Why do we create a temp2 list with the recent files instead of using recent_queue?
        This is because it is possible for recent_queue to contain files that are no
        longer in the regular file pool. This method ensures that no dead files are put
        into the queue.
        """
        new_queue = []

        # Fetch a list of the most recent files posted
        recent_queue = [row[0] for row in self.get_table_contents(self.recent_queue_table)]

        # Generate a list of files for the next queue
        response = self.client.list_objects(Bucket=self.bucket_name,Prefix=self.bucket_directory)
        file_pool = [file['Key'] for file in response['Contents'] if not file['Key'].endswith('/')]

        # Split the files into two groups, shuffle the first group
        temp = [row for row in file_pool if row not in recent_queue]
        temp2 = [row for row in file_pool if row in recent_queue] # SEE NOTE IN THE COMMENT ABOVE
        random.shuffle(temp)

        # Determine how many files to place at the front
        end = len(temp) if len(temp) < self.recent_limit else self.recent_limit
        for i in range(end):
            new_queue.append(temp.pop())

        # Form the rest of the queue, and shuffle again
        temp = temp + temp2
        random.shuffle(temp)

        # Finish creating the queue
        new_queue = new_queue + temp

        # Push the queue to the table
        conn = self.create_connection()
        cur = conn.cursor()

        for filepath in new_queue[::-1]:
            timestamp = str(datetime.datetime.now())
            cur.execute("INSERT INTO {0} (filepath, comment, timestamp) VALUES (%s, %s, %s)".format(self.queue_table), (filepath, None, timestamp))

        conn.commit()
        cur.close()
        conn.close()
        print("File queue {0} shuffled.".format(self.queue_table))


    # Counts the number of rows in the table, returns count as an integer
    def count_rows(self, table_name):
        conn = self.create_connection()
        cur = conn.cursor()

        cur.execute("SELECT count(*) FROM {}".format(table_name))

        count = cur.fetchone()[0]

        conn.commit()
        cur.close()
        conn.close()

        return count


    """
    Returns the newest row in the table (based on date of insertion)
    This used to return a single field as a string in previous builds, but will
    now return the entire row as a tuple.
    
    """
    def get_newest_row(self, table_name):
        conn = self.create_connection()
        cur = conn.cursor()

        cur.execute("SELECT * FROM {} ORDER BY timestamp DESC LIMIT 1".format(table_name))

        row = cur.fetchone()

        conn.commit()
        cur.close()
        conn.close()

        return row


    def delete_oldest_row(self, table_name, fieldname):
        """
        Delete oldest row in the table

        Takes table_name and fieldname strings. fieldname is the name of the column
        in the table called table_name which the function uses to order by date.
        Therefore, the column should be of an appropriate date type that can be ordered.

        Do not call this function on a table without a date field.
        """
        conn = self.create_connection()
        cur = conn.cursor()

        cur.execute("""DELETE FROM {0}
                       WHERE {1}
                       IN (SELECT {1}
                           FROM {0}
                           ORDER BY {1}
                           ASC
                           LIMIT 1)""".format(table_name, fieldname))

        conn.commit()
        cur.close()
        conn.close()


    # Delete a single row in the table
    def delete_row(self, table_name, field, id):
        conn = self.create_connection()
        cur = conn.cursor()

        cur.execute("DELETE FROM {0} WHERE {1} = ('{2}')".format(table_name, field, id))

        conn.commit()
        cur.close()
        conn.close()


    def insert_recent(self, entry):
        """
        Insert entry into a recent_queue table. Each row should have a path to an file
        and a timestamp of when the insertion occurred.

        The timestamp is provided by Python's datetime module.
        """
        conn = self.create_connection()
        cur = conn.cursor()

        timestamp = str(datetime.datetime.now())

        cur.execute("INSERT INTO {0} (filepath, timestamp) VALUES ('{1}','{2}')".format(self.recent_queue_table, entry, timestamp))

        conn.commit()
        cur.close()
        conn.close()


    # Helper function for creating a connection to the database
    def create_connection(self):
        parsed_url = urlparse(self.database_url)
        
        # Keep trying if the connection failed
        while True:
            try:
                return psycopg2.connect(database=parsed_url.path[1:],
                                        user=parsed_url.username,
                                        password=parsed_url.password,
                                        host=parsed_url.hostname,
                                        port=parsed_url.port)
            except psycopg2.OperationalError as error:
                # This can sometimes occur as "psycopg2.OperationalError: could not translate hostname" error
                # DNS Error?
                print("{0}: Could not connect to the database.".format(self.screen_name))
                


    # Check if the given id is in a request_sent table (returns either True or False)
    def request_sent(self, id):
        conn = self.create_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT id FROM {0} WHERE id = ('{1}')".format(self.request_sent_table, id))

        status = cur.fetchone() is not None

        conn.commit()
        cur.close()
        conn.close()

        return status


    # Push the id and screen name of the follower to the list of sent requests
    def update_request_sent(self, id, screen_name):
        conn = self.create_connection()
        cur = conn.cursor()
        
        timestamp = str(datetime.datetime.now())

        cur.execute("INSERT INTO {0} (id, screen_name, timestamp) VALUES ('{1}','{2}','{3}')".format(self.request_sent_table, id, screen_name, timestamp))

        conn.commit()
        cur.close()
        conn.close()


    # Get all rows and columns of a table
    def get_table_contents(self, table_name):
        conn = self.create_connection()
        cur = conn.cursor()

        entries = []

        cur.execute("SELECT * FROM {}".format(table_name))

        for row in cur.fetchall():
            entries.append(row)

        conn.commit()
        cur.close()
        conn.close()

        return entries
    
    """
    Get the timestamp of the most recently added row of the specified table
    The timestamp is returned as a datetime object!
    If the query returns None, it means that the recent_queue table is empty.
    In this case, the epoch time is returned. (1970-01-01 00:00:00)
    """
    def get_recent_timestamp(self, table_name):
        conn = self.create_connection()
        cur = conn.cursor()

        cur.execute("SELECT timestamp FROM {} ORDER BY timestamp DESC LIMIT 1".format(table_name))

        result = cur.fetchone()
        
        row = datetime.datetime.utcfromtimestamp(0) if result is None else result[0]

        conn.commit()
        cur.close()
        conn.close()

        return row
        
    # Get the time difference between now and when the most recent tweet was posted
    # Returns the seconds of the timedelta object!
    def get_time_since_last_tweet(self):
        timestamp = datetime.datetime.now()
        recent_tweet_timestamp = self.get_recent_timestamp(self.recent_queue_table)
        
        time_difference = timestamp - recent_tweet_timestamp
        
        return time_difference.total_seconds()
        
    """
    Checks if the bot is allowed to tweet
    
    1. tweet_enabled must be set to true in keys.json
    2. There must be at least one row in the queue
    3. A certain amount of time must have passed since the last tweet
       (defined as tweet_timeout in keys.json)
    
    """
    def can_tweet(self):
        return self.tweet_enabled and self.count_rows(self.queue_table) > 0 and self.get_time_since_last_tweet() > self.tweet_timeout